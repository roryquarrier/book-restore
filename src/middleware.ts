import { clerkMiddleware } from '@clerk/astro/server';
import { ensureProfile, type Profile } from './lib/supabase-server';

/** Routes that require a signed-in user. Prefix match. */
const PROTECTED_ROUTES = ['/dashboard', '/upload', '/jobs'];

/** Routes a signed-in user has no reason to see. */
const AUTH_ROUTES = ['/auth'];

const matches = (pathname: string, routes: string[]) =>
  routes.some((route) => pathname === route || pathname.startsWith(route + '/'));

// Wrap clerkMiddleware so we can attach profile data to locals
export const onRequest = clerkMiddleware(async (auth, context, next) => {
  const authObject = auth();

  const userId = authObject.userId;
  const email = authObject.sessionClaims?.email as string | undefined;

  console.log('[middleware]', context.url.pathname, '| userId:', userId, '| status:', (authObject as any).status);

  let profile: Profile | null = null;

  if (userId) {
    profile = await ensureProfile(userId, email ?? null);
  }

  context.locals.userId = userId ?? null;
  context.locals.email = email ?? null;
  context.locals.profile = profile;

  const { url } = context;

  if (!userId && matches(url.pathname, PROTECTED_ROUTES)) {
    // Only allow relative paths to prevent open redirect
    const target = url.pathname + url.search;
    return context.redirect('/auth?next=' + encodeURIComponent(target));
  }

  if (userId && matches(url.pathname, AUTH_ROUTES)) {
    return context.redirect('/dashboard');
  }

  return next();
});
