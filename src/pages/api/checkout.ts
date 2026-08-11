import type { APIRoute } from 'astro';
import {
  CREDIT_PACKS,
  formatPrice,
  isPackId,
  stripe,
  stripeConfigured,
} from '../../lib/stripe';

export const prerender = false;

const json = (body: unknown, status: number) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' },
  });

export const POST: APIRoute = async ({ locals, request }) => {
  if (!stripeConfigured) {
    console.error('[api/checkout] STRIPE_SECRET_KEY is not set');
    return json({ error: 'The shop is closed just now. Please try again shortly.' }, 503);
  }

  const userId = locals.userId;
  if (!userId) {
    return json({ error: 'You are not signed in. Sign in again, then choose a pack.' }, 401);
  }

  const email = locals.email ?? undefined;

  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return json({ error: 'That request was malformed — please choose a pack again.' }, 400);
  }

  const pack = (body as { pack?: unknown } | null)?.pack;
  if (!isPackId(pack)) {
    return json({ error: 'No such credit pack.' }, 400);
  }

  const chosen = CREDIT_PACKS[pack];
  const origin = new URL(request.url).origin;
  // Stable within a retry window but unique per purchase intent.
  // Client can send a request_id; if absent, fall back to timestamp so
  // double-clicks within the same second collapse but separate purchases don't.
  const requestId = (body as { request_id?: string } | null)?.request_id;
  const idempotencyKey = requestId
    ? userId + ':' + pack + ':' + requestId
    : userId + ':' + pack + ':' + Math.floor(Date.now() / 60000); // 1-min bucket

  const metadata = {
    user_id: userId,
    pack: chosen.id,
    credits: String(chosen.credits),
    idempotency_key: idempotencyKey,
  };

  try {
    const session = await stripe.checkout.sessions.create(
      {
        mode: 'payment',
        client_reference_id: userId,
        customer_email: email,
        line_items: [
          {
            quantity: 1,
            price_data: {
              currency: 'usd',
              unit_amount: chosen.priceCents,
              product_data: {
                name: 'Book Bash — ' + chosen.credits + ' restoration credits',
                description: chosen.label + ' · ' + formatPrice(chosen.priceCents) + ' for ' + chosen.credits + ' credits (about ' + chosen.credits + ' pages at 1 credit per page).',
              },
            },
          },
        ],
        metadata,
        payment_intent_data: { metadata },
        success_url: origin + '/dashboard?purchased=1',
        cancel_url: origin + '/dashboard?canceled=1',
      },
      { idempotencyKey }
    );

    if (!session.url) {
      console.error('[api/checkout] session created without a url', session.id);
      return json({ error: 'The payment desk did not open. Please try again.' }, 502);
    }

    return json({ url: session.url }, 200);
  } catch (error) {
    console.error('[api/checkout] session creation failed', error);
    return json({ error: 'The payment desk could not be reached. Please try again.' }, 502);
  }
};
