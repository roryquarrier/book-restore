/// <reference types="astro/client" />

export interface Profile {
  id: string;
  email: string | null;
  credits: number;
  pending_credits: number;
  created_at: string;
}

declare global {
  namespace App {
    interface Locals {
      userId: string | null;
      email: string | null;
      profile: Profile | null;
    }
  }
}

interface ImportMetaEnv {
  readonly PUBLIC_SUPABASE_URL: string;
  readonly PUBLIC_SUPABASE_ANON_KEY: string;
  readonly SUPABASE_SERVICE_ROLE_KEY?: string;
  readonly STRIPE_SECRET_KEY?: string;
  readonly STRIPE_WEBHOOK_SECRET?: string;
  readonly PUBLIC_CLERK_PUBLISHABLE_KEY: string;
  readonly CLERK_SECRET_KEY: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
