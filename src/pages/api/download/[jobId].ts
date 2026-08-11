import type { APIRoute } from 'astro';
import { getSupabaseServer } from '../../../lib/supabase-server';

export const prerender = false;

const BUCKET = 'book-bash';
const EXPIRES_IN = 60 * 60;

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

const json = (body: unknown, status: number) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' },
  });

function downloadName(sourceFilename: string | null): string {
  const base = (sourceFilename ?? 'restored.pdf').replace(/[/\\]/g, '_');
  const withExt = /\.pdf$/i.test(base) ? base : base + '.pdf';
  return withExt.startsWith('restored_') ? withExt : 'restored_' + withExt;
}

export const GET: APIRoute = async ({ params, locals, request, redirect }) => {
  const userId = locals.userId;
  if (!userId) {
    return json({ error: 'You are not signed in. Sign in again, then retry the download.' }, 401);
  }

  const jobId = params.jobId;
  if (!jobId || !UUID_RE.test(jobId)) {
    return json({ error: 'That is not a valid record number.' }, 400);
  }

  const supabase = getSupabaseServer();

  const { data: job, error: jobError } = await supabase
    .from('jobs')
    .select('id, status, source_filename, storage_path, output_storage_path, expires_at')
    .eq('id', jobId)
    .eq('user_id', userId)
    .maybeSingle();

  if (jobError) {
    console.error('[api/download] job lookup failed', jobError);
    return json({ error: 'The register could not be read just now. Please try again.' }, 502);
  }

  if (!job) {
    return json({ error: 'Nothing is filed under that record number.' }, 404);
  }

  if (job.status !== 'complete') {
    return json({ error: 'That restoration is not finished yet — there is nothing to collect.', status: job.status }, 409);
  }

  // Enforce the 7-day retention window. After expiry, the book is cleared
  // and the download link is refused even if the storage object lingers.
  // Fail closed: if expires_at is missing or unparseable, refuse the download.
  if (!job.expires_at) {
    return json({ error: 'This restoration has no retention record and cannot be collected.' }, 410);
  }
  const expiry = new Date(job.expires_at);
  if (isNaN(expiry.getTime()) || expiry.getTime() < Date.now()) {
    return json({ error: 'The retention window for this restoration has closed. Re-deposit the book to restore it again.' }, 410);
  }

  const path = job.output_storage_path ?? job.storage_path;
  if (!path) {
    return json({ error: 'No finished file is filed against that record.' }, 404);
  }

  const slash = path.lastIndexOf('/');
  const dir = slash === -1 ? '' : path.slice(0, slash);
  const name = slash === -1 ? path : path.slice(slash + 1);
  const { data: listing, error: listError } = await supabase.storage
    .from(BUCKET)
    .list(dir, { search: name, limit: 100 });

  if (listError) {
    console.error('[api/download] storage list failed', listError);
    return json({ error: 'The archive could not be reached just now. Please try again.' }, 502);
  }

  if (!listing?.some((entry) => entry.name === name)) {
    return json({ error: 'That file is no longer in the archive — cleared copies cannot be reissued.' }, 410);
  }

  const filename = downloadName(job.source_filename);
  const { data: signed, error: signError } = await supabase.storage
    .from(BUCKET)
    .createSignedUrl(path, EXPIRES_IN, { download: filename });

  if (signError || !signed?.signedUrl) {
    console.error('[api/download] signed url failed', signError);
    return json({ error: 'The collection link could not be issued. Please try again.' }, 502);
  }

  const url = new URL(request.url);
  if (url.searchParams.get('go') === '1') {
    return redirect(signed.signedUrl, 302);
  }

  return json({ url: signed.signedUrl, filename, expiresIn: EXPIRES_IN }, 200);
};
