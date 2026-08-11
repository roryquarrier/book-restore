import type { APIRoute } from 'astro';

export const prerender = false;

// With Clerk, sign-out is handled client-side by Clerk's <SignOutButton />.
// This endpoint is a server-side fallback that redirects to home.
export const GET: APIRoute = async ({ request }) => {
  const origin = new URL(request.url).origin;
  return Response.redirect(origin + '/', 302);
};

export const POST: APIRoute = async ({ request }) => {
  const origin = new URL(request.url).origin;
  return Response.redirect(origin + '/', 302);
};
