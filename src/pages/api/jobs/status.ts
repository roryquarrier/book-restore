import type { APIRoute } from 'astro';
import { getSupabaseServer } from '../../../lib/supabase-server';

export const prerender = false;

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

const ACTIVE = ['uploaded', 'extracting', 'regen_hold', 'regen_running', 'regen_complete'];

const json = (body: unknown, status: number) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' },
  });

const FIELDS =
  'id, status, source_filename, page_count, credits_held, credits_committed, error_message, created_at, updated_at, completed_at, expires_at, output_storage_path, storage_path';

const shape = (job: any) => ({
  id: job.id,
  status: job.status,
  source_filename: job.source_filename,
  page_count: job.page_count,
  credits_held: job.credits_held,
  credits_committed: job.credits_committed,
  error_message: job.error_message,
  created_at: job.created_at,
  updated_at: job.updated_at,
  completed_at: job.completed_at,
  expires_at: job.expires_at,
  active: ACTIVE.includes(job.status),
  downloadable: job.status === 'complete' && !!(job.output_storage_path ?? job.storage_path),
});

export const GET: APIRoute = async ({ locals, request }) => {
  const userId = locals.userId;

  if (!userId) {
    return json({ error: 'Not signed in.' }, 401);
  }

  const supabase = getSupabaseServer();
  const id = new URL(request.url).searchParams.get('id');

  if (id) {
    if (!UUID_RE.test(id)) {
      return json({ error: 'That is not a valid record number.' }, 400);
    }

    const { data: job, error } = await supabase
      .from('jobs')
      .select(FIELDS)
      .eq('id', id)
      .eq('user_id', userId)
      .maybeSingle();

    if (error) {
      console.error('[api/jobs/status] single lookup failed', error);
      return json({ error: 'The register could not be read just now.' }, 502);
    }
    if (!job) {
      return json({ error: 'Nothing is filed under that record number.' }, 404);
    }

    const { count } = await supabase
      .from('pages')
      .select('id', { count: 'exact', head: true })
      .eq('job_id', id)
      .eq('status', 'restored');

    const shaped = shape(job);
    return json(
      { job: shaped, progress: { done: count ?? 0, total: job.page_count ?? null }, anyActive: shaped.active },
      200
    );
  }

  const [{ data: jobs, error: jobsError }, { data: profile }] = await Promise.all([
    supabase.from('jobs').select(FIELDS).eq('user_id', userId).order('created_at', { ascending: false }).limit(25),
    supabase.from('profiles').select('credits, pending_credits').eq('id', userId).maybeSingle(),
  ]);

  if (jobsError) {
    console.error('[api/jobs/status] list failed', jobsError);
    return json({ error: 'The register could not be read just now.' }, 502);
  }

  const shaped = (jobs ?? []).map(shape);
  return json(
    { credits: profile?.credits ?? 0, pending: profile?.pending_credits ?? 0, jobs: shaped, anyActive: shaped.some((job) => job.active) },
    200
  );
};
