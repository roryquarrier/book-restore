import type { APIRoute } from 'astro';
import type Stripe from 'stripe';
import { createClient, type SupabaseClient } from '@supabase/supabase-js';
import { CREDIT_PACKS, isPackId, stripe, stripeConfigured, stripeWebhookSecret } from '../../lib/stripe';

export const prerender = false;

const text = (body: string, status: number) =>
  new Response(body, { status, headers: { 'Content-Type': 'text/plain' } });

/** Sessions Stripe considers settled. Everything else is credited later, or never. */
const CREDITING_EVENTS = ['checkout.session.completed', 'checkout.session.async_payment_succeeded'];

/** The PaymentIntent id, for the ledger's audit column. */
const paymentIntentId = (session: Stripe.Checkout.Session): string | null =>
  typeof session.payment_intent === 'string'
    ? session.payment_intent
    : (session.payment_intent?.id ?? null);

/** Postgres/PostgREST codes for "that function isn't in the schema". */
const MISSING_FUNCTION = ['42883', 'PGRST202'];
/** unique_violation on credit_transactions.idempotency_key. */
const UNIQUE_VIOLATION = '23505';

interface Grant {
  userId: string;
  credits: number;
  idempotencyKey: string;
  paymentIntent: string | null;
}

/**
 * Awards credits once. Returns false when this purchase was already credited.
 *
 * The RPC from migration 003 is the real path: it writes the ledger row and
 * moves the balance in one transaction. Until that migration is applied the
 * fallback below does the same two writes from here — still safe against
 * duplicate deliveries (the UNIQUE index on idempotency_key is what decides),
 * but not against a crash landing between them.
 */
async function grantCredits(admin: SupabaseClient, grant: Grant): Promise<boolean> {
  const { data, error } = await admin.rpc('grant_purchase_credits', {
    p_user_id: grant.userId,
    p_amount: grant.credits,
    p_idempotency_key: grant.idempotencyKey,
    p_payment_intent: grant.paymentIntent,
  });

  if (!error) return Boolean(data);
  if (!MISSING_FUNCTION.includes(error.code ?? '')) throw error;

  console.warn(
    '[stripe-webhook] grant_purchase_credits() is missing — apply supabase/migrations/003_grant_purchase_credits.sql. Falling back to a non-atomic grant.'
  );

  // Ledger row first: the unique key is the gate, so a redelivery stops here.
  const { error: insertError } = await admin.from('credit_transactions').insert({
    user_id: grant.userId,
    amount: grant.credits,
    reason: 'stripe_purchase',
    stripe_payment_intent: grant.paymentIntent,
    idempotency_key: grant.idempotencyKey,
  });

  if (insertError) {
    if (insertError.code === UNIQUE_VIOLATION) return false;
    throw insertError;
  }

  const { data: profile, error: readError } = await admin
    .from('profiles')
    .select('credits')
    .eq('id', grant.userId)
    .single();

  if (readError) throw readError;

  const { error: updateError } = await admin
    .from('profiles')
    .update({ credits: (profile?.credits ?? 0) + grant.credits })
    .eq('id', grant.userId);

  if (updateError) throw updateError;

  return true;
}

/**
 * Stripe's side of the counter.
 *
 * The signature is checked against the raw body before anything is read out of
 * it — the request is public and otherwise unauthenticated, so an unverified
 * payload is treated as hostile. Credits are then granted through
 * grant_purchase_credits(), which inserts the ledger row and moves the balance
 * in one transaction keyed on idempotency_key, so a redelivered event is a
 * no-op rather than a second helping of credits.
 *
 * Anything we can't act on still answers 200: a non-2xx tells Stripe to retry,
 * and retrying won't fix a malformed event. Only genuine "try me again later"
 * failures (the database being unreachable) return 500.
 */
export const POST: APIRoute = async ({ request }) => {
  if (!stripeConfigured) {
    console.error('[stripe-webhook] STRIPE_SECRET_KEY is not set');
    return text('stripe not configured', 500);
  }

  if (!stripeWebhookSecret) {
    // Fill STRIPE_WEBHOOK_SECRET in .env with the `whsec_…` value from
    // Stripe Dashboard → Developers → Webhooks → your endpoint (or from
    // `stripe listen --forward-to localhost:4321/api/stripe-webhook`).
    console.error('[stripe-webhook] STRIPE_WEBHOOK_SECRET is empty — cannot verify signatures');
    return text('webhook secret not configured', 500);
  }

  const signature = request.headers.get('stripe-signature');
  if (!signature) {
    return text('missing stripe-signature header', 400);
  }

  // Must be the exact bytes Stripe signed — never request.json() here.
  const payload = await request.text();

  let event: Stripe.Event;
  try {
    event = await stripe.webhooks.constructEventAsync(payload, signature, stripeWebhookSecret);
  } catch (error) {
    console.error('[stripe-webhook] signature verification failed', error);
    return text('invalid signature', 400);
  }

  if (!CREDITING_EVENTS.includes(event.type)) {
    return text('ignored', 200);
  }

  const session = event.data.object as Stripe.Checkout.Session;

  if (session.payment_status !== 'paid') {
    // A delayed payment method that hasn't cleared; the async_payment_succeeded
    // event will arrive if and when it does.
    return text('not paid yet', 200);
  }

  const metadata = session.metadata ?? {};
  const userId = metadata.user_id || session.client_reference_id;
  if (!userId) {
    console.error('[stripe-webhook] no user on session', session.id);
    return text('no user on session', 200);
  }

  // Prefer the pack's own credit count over the metadata number, so a tampered
  // or stale session can't ask for more than the pack is worth.
  const pack = isPackId(metadata.pack) ? CREDIT_PACKS[metadata.pack] : null;
  const credits = pack?.credits ?? Number(metadata.credits);
  if (!Number.isInteger(credits) || credits <= 0) {
    console.error('[stripe-webhook] no credit amount on session', session.id, metadata);
    return text('no credit amount on session', 200);
  }

  // Sessions created before this metadata existed still get a stable key.
  const idempotencyKey = metadata.idempotency_key || `session:${session.id}`;

  // Vite's import.meta.env doesn't see Fly.io runtime secrets, so fall back to
  // process.env (where Fly.io injects them). Same pattern as lib/stripe.ts.
  const nodeEnv =
    (globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env ?? {};
  const readEnv = (name: string): string | undefined =>
    (import.meta.env as Record<string, string | undefined>)[name] || nodeEnv[name];

  const supabaseUrl = readEnv('PUBLIC_SUPABASE_URL');
  const serviceRoleKey = readEnv('SUPABASE_SERVICE_ROLE_KEY');
  if (!supabaseUrl || !serviceRoleKey) {
    console.error('[stripe-webhook] PUBLIC_SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY is not set');
    return text('supabase not configured', 500);
  }

  // The webhook has no session cookies, so it writes as the service role —
  // RLS gives an anonymous caller no path to another user's profile.
  const admin = createClient(supabaseUrl, serviceRoleKey, {
    auth: { persistSession: false, autoRefreshToken: false },
  });

  let granted: boolean;
  try {
    granted = await grantCredits(admin, {
      userId,
      credits,
      idempotencyKey,
      paymentIntent: paymentIntentId(session),
    });
  } catch (error) {
    // Genuinely retryable — ask Stripe to send it again.
    console.error('[stripe-webhook] could not grant credits', session.id, error);
    return text('could not record the purchase', 500);
  }

  if (granted) {
    console.log(`[stripe-webhook] credited ${credits} to ${userId} (${session.id})`);
  } else {
    console.log(`[stripe-webhook] duplicate delivery ignored (${session.id})`);
  }

  return text('ok', 200);
};
