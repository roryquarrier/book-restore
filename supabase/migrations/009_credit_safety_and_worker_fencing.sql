-- Migration 009: Credit safety (status gating + idempotency) and worker fencing
--
-- Fixes three confirmed money bugs:
--
-- A1. The dead-man switch (worker.reclaim_stale_jobs) races the worker it is
--     reclaiming from. A heartbeat hiccup >5 min made the reclaimer refund the
--     user and re-queue the job while the original worker was still running.
--     That worker's later commit_credits()/refund_credits() then moved the
--     balance a second time — a free restore, or +N credits out of thin air.
--     Fix: jobs carry a lease (worker_id + job_generation). Every credit RPC
--     takes the caller's lease and no-ops (RETURNS FALSE) if it has moved on.
--
-- B4. hold/commit/refund_credits acted on any job in any status, so a
--     duplicate call double-held, double-committed or double-refunded.
--     Fix: each function gates on the statuses it is allowed to act from, and
--     the state change it makes takes the job out of that set — so a second
--     call is a no-op. Both checks happen under a FOR UPDATE row lock on the
--     job, which is what serialises two workers racing on the same job.
--
-- A3. grant_purchase_credits() wrote the ledger row (burning the Stripe
--     idempotency key) and only then discovered the profile did not exist,
--     returning FALSE. The webhook read FALSE as "duplicate delivery",
--     answered 200, and Stripe never retried: the customer paid and got
--     nothing, with no way to recover. Fix: create the profile first, and
--     RAISE on anything that would leave the ledger row without a matching
--     balance change, so the whole transaction rolls back and the webhook's
--     500 makes Stripe retry.

-- ============================================
-- 1. LEASE COLUMNS ON jobs
-- ============================================
-- worker_id      — the worker that currently holds the job, NULL when queued.
-- job_generation — bumped on every (re)claim. A worker captures it when it
--                  claims and passes it back with every write; a stale worker's
--                  writes no longer match and are silently dropped.

ALTER TABLE public.jobs ADD COLUMN IF NOT EXISTS worker_id TEXT;
ALTER TABLE public.jobs ADD COLUMN IF NOT EXISTS job_generation INTEGER NOT NULL DEFAULT 0;

-- Partial index to accelerate the dead-man switch scan, which filters on
-- in-flight statuses + staleness every poll. Only a handful of rows are
-- in-flight at any time, so this index is tiny and fast.
CREATE INDEX IF NOT EXISTS idx_jobs_inflight
  ON public.jobs (status, updated_at)
  WHERE status IN ('extracting', 'regen_hold', 'regen_running');

-- ============================================
-- 2. DROP THE UNFENCED CREDIT FUNCTIONS
-- ============================================
-- The new versions take two more arguments. The old two-argument signatures
-- must go, or a call naming only p_job_id/p_amount is ambiguous (42725).
--
-- DEPLOY ORDER (critical): Apply this migration AFTER deploying the new
-- worker code that passes worker_id + generation. During the brief window
-- between DROP and the new CREATE, any in-flight RPC call from the old worker
-- will fail. For the current single-worker deployment, stop the worker, apply
-- the migration, deploy the new worker, then start it. Existing in-flight jobs
-- will be reclaimed by the new worker's dead-man switch after STALE_AFTER.

DROP FUNCTION IF EXISTS public.hold_credits(UUID, INTEGER);
DROP FUNCTION IF EXISTS public.commit_credits(UUID, INTEGER);
DROP FUNCTION IF EXISTS public.refund_credits(UUID, INTEGER);

-- ============================================
-- 3. FENCED, STATUS-GATED CREDIT FUNCTIONS
-- ============================================
-- p_worker_id / p_generation are the caller's lease. NULL means "unfenced" and
-- is only for manual/admin repair from the SQL editor — the worker always
-- passes both. Even unfenced, the status gate below still prevents a double
-- hold, commit or refund.
--
-- Every function locks the job row (SELECT … FOR UPDATE) *before* touching a
-- balance, and returns FALSE without writing anything if the gate fails. Two
-- concurrent callers therefore queue on the job row and the second one
-- re-evaluates the gate against the first one's committed state.

-- Hold credits for a job: available → pending. Only from a freshly claimed
-- job that is not already holding anything, so it can only run once.
CREATE OR REPLACE FUNCTION public.hold_credits(
  p_job_id UUID,
  p_amount INTEGER,
  p_worker_id TEXT DEFAULT NULL,
  p_generation INTEGER DEFAULT NULL
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_user_id TEXT;
BEGIN
  IF p_amount IS NULL OR p_amount <= 0 THEN
    RETURN FALSE;
  END IF;

  SELECT user_id INTO v_user_id
  FROM public.jobs
  WHERE id = p_job_id
    AND status IN ('extracting', 'regen_hold')
    AND credits_held = 0
    AND (p_worker_id IS NULL OR worker_id = p_worker_id)
    AND (p_generation IS NULL OR job_generation = p_generation)
  FOR UPDATE;

  -- Wrong status, already holding, or this worker's lease has expired.
  IF NOT FOUND OR v_user_id IS NULL THEN
    RETURN FALSE;
  END IF;

  UPDATE public.profiles
  SET credits = credits - p_amount,
      pending_credits = pending_credits + p_amount
  WHERE id = v_user_id AND credits >= p_amount;

  -- Short balance (or no profile). Nothing has been written yet, so FALSE
  -- leaves the job exactly as it was found.
  IF NOT FOUND THEN
    RETURN FALSE;
  END IF;

  UPDATE public.jobs SET
    credits_held = p_amount,
    status = 'regen_hold',
    updated_at = now()
  WHERE id = p_job_id;

  INSERT INTO public.credit_transactions (user_id, amount, reason, job_id)
  VALUES (v_user_id, -p_amount, 'regen_hold', p_job_id);

  RETURN TRUE;
END;
$$;

-- Commit the hold on success: pending → spent. Only from 'regen_running', and
-- the commit moves the job to 'complete', so a second call finds nothing.
CREATE OR REPLACE FUNCTION public.commit_credits(
  p_job_id UUID,
  p_amount INTEGER,
  p_worker_id TEXT DEFAULT NULL,
  p_generation INTEGER DEFAULT NULL
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_user_id TEXT;
BEGIN
  SELECT user_id INTO v_user_id
  FROM public.jobs
  WHERE id = p_job_id
    AND status = 'regen_running'
    AND (p_worker_id IS NULL OR worker_id = p_worker_id)
    AND (p_generation IS NULL OR job_generation = p_generation)
  FOR UPDATE;

  -- Already committed/refunded, or the dead-man switch took the job away.
  IF NOT FOUND OR v_user_id IS NULL THEN
    RETURN FALSE;
  END IF;

  UPDATE public.profiles
  SET pending_credits = GREATEST(0, pending_credits - p_amount)
  WHERE id = v_user_id;

  UPDATE public.jobs SET
    credits_committed = p_amount,
    credits_held = 0,
    status = 'complete',
    completed_at = now(),
    updated_at = now()
  WHERE id = p_job_id;

  -- Audit row: amount is 0 because hold_credits already moved the money.
  INSERT INTO public.credit_transactions (user_id, amount, reason, job_id)
  VALUES (v_user_id, 0, 'regen_commit', p_job_id);

  RETURN TRUE;
END;
$$;

-- Return a hold on failure: pending → available. Only from a status that can
-- still be holding credits; the refund moves the job to 'refunded' and clears
-- credits_held, so a second call — or a call against an already completed job —
-- is a no-op.
CREATE OR REPLACE FUNCTION public.refund_credits(
  p_job_id UUID,
  p_amount INTEGER,
  p_worker_id TEXT DEFAULT NULL,
  p_generation INTEGER DEFAULT NULL
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_user_id TEXT;
BEGIN
  SELECT user_id INTO v_user_id
  FROM public.jobs
  WHERE id = p_job_id
    AND status IN ('regen_hold', 'regen_running')
    AND credits_held > 0
    AND (p_worker_id IS NULL OR worker_id = p_worker_id)
    AND (p_generation IS NULL OR job_generation = p_generation)
  FOR UPDATE;

  IF NOT FOUND OR v_user_id IS NULL THEN
    RETURN FALSE;
  END IF;

  UPDATE public.profiles
  SET credits = credits + p_amount,
      pending_credits = GREATEST(0, pending_credits - p_amount)
  WHERE id = v_user_id;

  UPDATE public.jobs SET
    status = 'refunded',
    credits_held = 0,
    updated_at = now()
  WHERE id = p_job_id;

  INSERT INTO public.credit_transactions (user_id, amount, reason, job_id)
  VALUES (v_user_id, p_amount, 'regen_refund', p_job_id);

  RETURN TRUE;
END;
$$;

-- ============================================
-- 4. ATOMIC PURCHASE GRANT
-- ============================================
-- The contract with the Stripe webhook:
--   TRUE      → credited now.
--   FALSE     → already credited (or an unusable event); safe to answer 200.
--   EXCEPTION → nothing happened, the whole transaction rolled back including
--               the ledger row, so the idempotency key is still free. The
--               webhook answers 500 and Stripe redelivers.
-- The old version could burn the key without moving the balance, which read as
-- FALSE ("duplicate") and permanently lost the customer's purchase.

CREATE OR REPLACE FUNCTION public.grant_purchase_credits(
  p_user_id TEXT,
  p_amount INTEGER,
  p_idempotency_key TEXT,
  p_payment_intent TEXT DEFAULT NULL
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  IF p_amount IS NULL OR p_amount <= 0
     OR p_idempotency_key IS NULL OR p_idempotency_key = ''
     OR p_user_id IS NULL OR p_user_id = '' THEN
    RETURN FALSE;
  END IF;

  -- The profile has to exist before the ledger row is written, otherwise a
  -- customer who pays before their first sign-in lands writes the key and
  -- loses the credits. Same shape as ensure_profile(): a brand new profile
  -- starts on the 10-credit signup bonus, and ensure_profile() will see the
  -- conflict on their first sign-in and not grant it twice.
  INSERT INTO public.profiles (id, credits)
  VALUES (p_user_id, 10)
  ON CONFLICT (id) DO NOTHING;

  IF FOUND THEN
    INSERT INTO public.credit_transactions (user_id, amount, reason)
    VALUES (p_user_id, 10, 'signup_bonus');
  END IF;

  INSERT INTO public.credit_transactions
    (user_id, amount, reason, stripe_payment_intent, idempotency_key)
  VALUES
    (p_user_id, p_amount, 'stripe_purchase', p_payment_intent, p_idempotency_key)
  ON CONFLICT (idempotency_key) DO NOTHING;

  -- The key is taken: this delivery was already credited.
  IF NOT FOUND THEN
    RETURN FALSE;
  END IF;

  UPDATE public.profiles
  SET credits = credits + p_amount
  WHERE id = p_user_id;

  -- Unreachable short of a concurrent profile delete. Raising (rather than
  -- returning FALSE) is the whole point of this migration: it rolls the ledger
  -- row back so Stripe's retry can still credit the purchase.
  IF NOT FOUND THEN
    RAISE EXCEPTION
      'grant_purchase_credits: no profile % to credit — rolling back so Stripe retries',
      p_user_id;
  END IF;

  RETURN TRUE;
END;
$$;

-- ============================================
-- 4b. ATOMIC STALE-JOB RECLAIM
-- ============================================
-- Fences the old worker (bumps generation), refunds whatever credits_held
-- actually is on the locked row (never the pre-scan snapshot), and re-queues
-- the job — all in one transaction so a crash mid-reclaim can't leave the job
-- refunded-but-not-requeued, or re-queued with credits still in pending.
--
-- Returns the job's id on success, NULL if the job was no longer in-flight
-- (already completed/abandoned by the time we got the lock).

CREATE OR REPLACE FUNCTION public.reclaim_stale_job(
  p_job_id UUID,
  p_worker_id TEXT,
  p_generation INTEGER
)
RETURNS UUID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_row RECORD;
BEGIN
  -- Lock the job row. The status+generation check ensures we only reclaim
  -- jobs that are genuinely in-flight at the generation we scanned.
  SELECT user_id, credits_held, status, job_generation
    INTO v_row
  FROM public.jobs
  WHERE id = p_job_id
    AND status IN ('extracting', 'regen_hold', 'regen_running')
    AND job_generation = p_generation
  FOR UPDATE;

  -- Job already moved on (completed, refunded, or reclaimed by someone else).
  IF NOT FOUND THEN
    RETURN NULL;
  END IF;

  -- Refund whatever is actually held on the locked row, using the row's own
  -- credits_held — not the amount the caller passed in. This closes the
  -- "stale snapshot" window where hold_credits landed between scan and reclaim.
  IF v_row.credits_held > 0 AND v_row.user_id IS NOT NULL THEN
    UPDATE public.profiles
    SET credits = credits + v_row.credits_held,
        pending_credits = GREATEST(0, pending_credits - v_row.credits_held)
    WHERE id = v_row.user_id;

    INSERT INTO public.credit_transactions (user_id, amount, reason, job_id)
    VALUES (v_row.user_id, v_row.credits_held, 'regen_refund', p_job_id);
  END IF;

  -- Re-queue: clear the lease, zero the hold, bump the generation so the old
  -- worker's in-flight writes don't match, and put the job back in the queue.
  UPDATE public.jobs SET
    status = 'uploaded',
    credits_held = 0,
    worker_id = NULL,
    job_generation = v_row.job_generation + 1,
    error_message = NULL,
    updated_at = now()
  WHERE id = p_job_id;

  RETURN p_job_id;
END;
$$;

-- ============================================
-- 5. LOCK DOWN THE NEW SIGNATURES
-- ============================================
-- Same pattern as migration 006: only service_role (the app backend and the
-- worker) may call these. The GRANTs from 006 died with the dropped functions.

REVOKE ALL ON FUNCTION public.hold_credits(UUID, INTEGER, TEXT, INTEGER) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.hold_credits(UUID, INTEGER, TEXT, INTEGER) FROM anon;
REVOKE ALL ON FUNCTION public.hold_credits(UUID, INTEGER, TEXT, INTEGER) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.hold_credits(UUID, INTEGER, TEXT, INTEGER) TO service_role;

REVOKE ALL ON FUNCTION public.commit_credits(UUID, INTEGER, TEXT, INTEGER) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.commit_credits(UUID, INTEGER, TEXT, INTEGER) FROM anon;
REVOKE ALL ON FUNCTION public.commit_credits(UUID, INTEGER, TEXT, INTEGER) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.commit_credits(UUID, INTEGER, TEXT, INTEGER) TO service_role;

REVOKE ALL ON FUNCTION public.refund_credits(UUID, INTEGER, TEXT, INTEGER) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.refund_credits(UUID, INTEGER, TEXT, INTEGER) FROM anon;
REVOKE ALL ON FUNCTION public.refund_credits(UUID, INTEGER, TEXT, INTEGER) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.refund_credits(UUID, INTEGER, TEXT, INTEGER) TO service_role;

REVOKE ALL ON FUNCTION public.grant_purchase_credits(TEXT, INTEGER, TEXT, TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.grant_purchase_credits(TEXT, INTEGER, TEXT, TEXT) FROM anon;
REVOKE ALL ON FUNCTION public.grant_purchase_credits(TEXT, INTEGER, TEXT, TEXT) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.grant_purchase_credits(TEXT, INTEGER, TEXT, TEXT) TO service_role;

REVOKE ALL ON FUNCTION public.reclaim_stale_job(UUID, TEXT, INTEGER) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.reclaim_stale_job(UUID, TEXT, INTEGER) FROM anon;
REVOKE ALL ON FUNCTION public.reclaim_stale_job(UUID, TEXT, INTEGER) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.reclaim_stale_job(UUID, TEXT, INTEGER) TO service_role;
