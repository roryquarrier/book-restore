# Book Bash — Clean copies of any book.

Web service for ESL teachers: upload scanned book PDFs, get clean restored copies.

## Stack
- **Frontend:** Astro 7 + React 19 + Tailwind 4
- **Backend:** Supabase (Postgres, Auth, Storage)
- **Payments:** Stripe Checkout (credit packs)
- **Worker:** Python (restore.py + queue consumer)
- **Image API:** OpenAI gpt-image-2

## Local Development

### Frontend (Astro)
```bash
cd ~/book-bash
npm install
npm run dev    # http://localhost:4321
```

### Worker (Python)
```bash
cd ~/book-bash/worker
bash start.sh
```

### Environment (.env)
- PUBLIC_SUPABASE_URL / PUBLIC_SUPABASE_ANON_KEY — browser-safe
- SUPABASE_SERVICE_ROLE_KEY — server-only
- DATABASE_URL — Postgres connection
- OPENAI_API_KEY — for restore.py
- STRIPE_SECRET_KEY — payment processing
- STRIPE_WEBHOOK_SECRET — webhook verification (get via `stripe listen --forward-to localhost:4321/api/stripe-webhook`)

## Project Structure
```
book-bash/
├── src/
│   ├── pages/
│   │   ├── index.astro        # Landing page (v2 Archival Museum)
│   │   ├── auth.astro         # Sign in / sign up
│   │   ├── dashboard.astro    # Credits, jobs, buy credits
│   │   ├── upload.astro       # PDF upload (museum intake desk)
│   │   ├── jobs/[id].astro    # Job status + download
│   │   └── api/
│   │       ├── upload.ts      # Server-side upload fallback
│   │       ├── checkout.ts    # Stripe Checkout
│   │       ├── stripe-webhook.ts  # Stripe webhook handler
│   │       ├── download/[jobId].ts # Signed download URL
│   │       └── signout.ts     # Sign out
│   ├── lib/
│   │   ├── supabase.ts        # Browser client
│   │   ├── supabase-server.ts # Server client
│   │   └── stripe.ts          # Stripe client + credit packs
│   ├── middleware.ts          # Route protection
│   ├── layouts/Base.astro
│   └── styles/global.css      # --tv-* tokens
├── worker/
│   ├── worker.py              # Queue consumer + restore engine
│   ├── requirements.txt
│   └── start.sh
├── supabase/migrations/       # 3 SQL migrations
└── public/images/             # Beatrix Potter hero + before/after
```

## Database Migrations
1. `001_initial_schema.sql` — tables, RLS, credit functions, storage bucket
2. `002_fix_commit_credits.sql` — fixes audit ledger double-counting
3. `003_grant_purchase_credits.sql` — atomic credit purchase function

## Credit System
- 10 free credits on signup
- 3 credits per page restored
- $5 = 50 credits · $10 = 120 credits · $20 = 260 credits
- Hold → commit on success → refund on failure
