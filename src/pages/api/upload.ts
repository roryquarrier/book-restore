import type { APIRoute } from 'astro';
import { getSupabaseServer } from '../../lib/supabase-server';

export const prerender = false;

const BUCKET = 'book-bash';
const MAX_BYTES = 50 * 1024 * 1024;

const json = (body: unknown, status: number) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });

function safeName(name: string): string {
  const cleaned = name
    .normalize('NFKD')
    .replace(/[^\w.\-]+/g, '_')
    .replace(/_{2,}/g, '_')
    .replace(/^[._]+/, '');
  const fallback = cleaned || 'document.pdf';
  return fallback.length > 120 ? fallback.slice(-120) : fallback;
}

export const POST: APIRoute = async ({ locals, request }) => {
  const userId = locals.userId;
  if (!userId) {
    return json({ error: 'You are not signed in. Sign in again, then retry the deposit.' }, 401);
  }

  // Reject oversized uploads BEFORE buffering the body into memory
  const contentLength = Number(request.headers.get('content-length') ?? '0');
  if (contentLength > MAX_BYTES + 1024) {
    return json({ error: 'The upload exceeds the 50 MB limit. Deposit the book in parts.' }, 413);
  }

  const supabase = getSupabaseServer();

  let form: FormData;
  try {
    form = await request.formData();
  } catch {
    return json({ error: 'The upload was malformed — please choose the PDF again.' }, 400);
  }

  const file = form.get('file');
  if (!(file instanceof File)) {
    return json({ error: 'No file arrived with the request. Choose a PDF and try again.' }, 400);
  }

  const looksPdf = file.type === 'application/pdf' || /\.pdf$/i.test(file.name);
  if (!looksPdf) {
    return json({ error: '"' + file.name + '" is not a PDF. The intake desk accepts PDF scans only.' }, 415);
  }
  // Verify magic bytes — don't trust the Content-Type header alone
  const header = new Uint8Array(await file.slice(0, 5).arrayBuffer());
  const isPdf = header[0] === 0x25 && header[1] === 0x50 && header[2] === 0x44 && header[3] === 0x46;
  if (!isPdf) {
    return json({ error: '"' + file.name + '" does not contain a valid PDF signature. Export it as a PDF and try again.' }, 415);
  }
  if (file.size === 0) {
    return json({ error: '"' + file.name + '" is empty — nothing was read from it.' }, 400);
  }
  if (file.size > MAX_BYTES) {
    return json({ error: '"' + file.name + '" is ' + (file.size / (1024 * 1024)).toFixed(1) + ' MB. The limit is 50 MB — deposit the book in parts.' }, 413);
  }

  const path = userId + '/' + Date.now() + '_' + safeName(file.name);

  const { error: uploadError } = await supabase.storage
    .from(BUCKET)
    .upload(path, file, { contentType: 'application/pdf', upsert: false });

  if (uploadError) {
    console.error('[api/upload] storage upload failed', uploadError);
    return json({ error: 'The archive would not take the file just now. Please try the deposit again.' }, 502);
  }

  const { data: job, error: insertError } = await supabase
    .from('jobs')
    .insert({
      user_id: userId,
      status: 'uploaded',
      source_filename: file.name,
      storage_path: path,
    })
    .select('id')
    .single();

  if (insertError || !job) {
    await supabase.storage.from(BUCKET).remove([path]);
    console.error('[api/upload] job insert failed', insertError);
    return json({ error: 'The file arrived but could not be entered in the register. Please try again.' }, 500);
  }

  return json({ jobId: job.id, storagePath: path, status: 'uploaded' }, 201);
};
