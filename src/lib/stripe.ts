import Stripe from 'stripe';

/**
 * Vite loads .env into import.meta.env, not process.env, so read both:
 * import.meta.env covers `astro dev`/`astro build`, process.env covers a
 * standalone node server started with real environment variables.
 */
// @types/node isn't installed, so reach process.env through globalThis.
const nodeEnv =
  (globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env ?? {};

/** An empty value in .env counts as unset, so a real environment variable can win. */
const readEnv = (name: string): string =>
  (import.meta.env as Record<string, string | undefined>)[name] || nodeEnv[name] || '';

const secretKey = readEnv('STRIPE_SECRET_KEY');

/** Empty until an endpoint is created in the Stripe dashboard — see the note in .env. */
export const stripeWebhookSecret = readEnv('STRIPE_WEBHOOK_SECRET');

/** Endpoints check this before touching `stripe` so a missing key is a 503, not a crash. */
export const stripeConfigured = Boolean(secretKey);

/**
 * The shop is server-only: this module must never be imported from a client
 * component or the secret key ends up in the browser bundle.
 */
export const stripe = new Stripe(secretKey, {
  appInfo: { name: 'Book Bash', url: 'https://bookbash.app' },
});

export type PackId = 'small' | 'medium' | 'large';

export interface CreditPack {
  id: PackId;
  /** Charged in cents — Stripe's unit_amount. */
  priceCents: number;
  credits: number;
  label: string;
  /** Museum-register phrasing for the acquisition cards. */
  note: string;
}

/**
 * Three tiers of acquisition. Price per credit falls as the tier rises;
 * at 3 credits per page, `credits / 3` is the page allowance shown on the card.
 */
export const CREDIT_PACKS: Record<PackId, CreditPack> = {
  small: {
    id: 'small',
    priceCents: 500,
    credits: 50,
    label: 'Single Volume',
    note: 'A short book, or a trial of the process.',
  },
  medium: {
    id: 'medium',
    priceCents: 1000,
    credits: 120,
    label: 'Standing Order',
    note: 'The usual deposit — a full-length book with room to spare.',
  },
  large: {
    id: 'large',
    priceCents: 2000,
    credits: 260,
    label: 'Whole Shelf',
    note: 'Several books, or one long one restored end to end.',
  },
};

export const PACK_IDS = Object.keys(CREDIT_PACKS) as PackId[];

export const isPackId = (value: unknown): value is PackId =>
  typeof value === 'string' && Object.hasOwn(CREDIT_PACKS, value);

/** "$5.00" — used on the acquisition cards and in the Stripe line item. */
export const formatPrice = (cents: number): string =>
  cents % 100 === 0 ? `$${cents / 100}` : `$${(cents / 100).toFixed(2)}`;

/** "$0.10 / credit" — the comparison that makes the tiers legible. */
export const perCredit = (pack: CreditPack): string =>
  `$${(pack.priceCents / 100 / pack.credits).toFixed(3).replace(/0$/, '')}`;
