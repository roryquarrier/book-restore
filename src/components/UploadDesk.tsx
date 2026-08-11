import { useRef, useState, type ChangeEvent, type DragEvent } from 'react';

const MAX_BYTES = 50 * 1024 * 1024;
const CREDITS_PER_PAGE = 1;

interface Props {
  userId: string;
  credits: number;
}

type Phase = 'idle' | 'chosen' | 'uploading' | 'filing' | 'done';

function safeName(name: string): string {
  const cleaned = name
    .normalize('NFKD')
    .replace(/[^\w.\-]+/g, '_')
    .replace(/_{2,}/g, '_')
    .replace(/^[._]+/, '');
  const fallback = cleaned || 'document.pdf';
  return fallback.length > 120 ? fallback.slice(-120) : fallback;
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

function rejectionReason(file: File): string | null {
  const looksPdf = file.type === 'application/pdf' || /\.pdf$/i.test(file.name);
  if (!looksPdf) {
    return '"' + file.name + '" is not a PDF. The intake desk accepts PDF scans only — if you have images or a Word file, export it as a PDF first.';
  }
  if (file.type && file.type !== 'application/pdf') {
    return '"' + file.name + '" says it is a ' + file.type + ' file. Only PDFs can be accepted.';
  }
  if (file.size === 0) {
    return '"' + file.name + '" is empty — nothing was read from it. Try exporting the scan again.';
  }
  if (file.size > MAX_BYTES) {
    return '"' + file.name + '" is ' + formatSize(file.size) + '. The limit is 50 MB — split the book into volumes, or lower the scan resolution, and deposit it in parts.';
  }
  return null;
}

function uploadViaServer(file: File, onProgress: (percent: number) => void): Promise<string> {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    form.append('file', file);

    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/upload', true);
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) {
        onProgress(Math.round((event.loaded / event.total) * 100));
      }
    };
    xhr.onload = () => {
      let body: any = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        /* fall through */
      }
      if (xhr.status >= 200 && xhr.status < 300 && body?.jobId) {
        resolve(body.jobId as string);
        return;
      }
      reject(new Error(body?.error ?? 'The intake desk could not accept the file (' + xhr.status + ').'));
    };
    xhr.onerror = () => reject(new Error('The connection dropped mid-transfer.'));
    xhr.send(form);
  });
}

export default function UploadDesk({ userId, credits }: Props) {
  const [file, setFile] = useState<File | null>(null);
  const [phase, setPhase] = useState<Phase>('idle');
  const [percent, setPercent] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const dragDepth = useRef(0);

  const busy = phase === 'uploading' || phase === 'filing' || phase === 'done';

  function accept(candidate: File | null | undefined) {
    if (!candidate) return;
    const reason = rejectionReason(candidate);
    if (reason) {
      setError(reason);
      setFile(null);
      setPhase('idle');
      return;
    }
    setError(null);
    setFile(candidate);
    setPercent(0);
    setPhase('chosen');
  }

  function handleDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    dragDepth.current = 0;
    setDragging(false);
    if (busy) return;
    const dropped = event.dataTransfer.files;
    if (dropped.length > 1) {
      setError('One book at a time, please. Deposit the first PDF, then come back for the next.');
      return;
    }
    accept(dropped[0]);
  }

  function handleDragEnter(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    if (busy) return;
    dragDepth.current += 1;
    setDragging(true);
  }

  function handleDragLeave(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    dragDepth.current = Math.max(0, dragDepth.current - 1);
    if (dragDepth.current === 0) setDragging(false);
  }

  function handleChange(event: ChangeEvent<HTMLInputElement>) {
    accept(event.target.files?.[0]);
    event.target.value = '';
  }

  function clearFile() {
    setFile(null);
    setError(null);
    setPercent(0);
    setPhase('idle');
  }

  async function handleSubmit() {
    if (!file || busy) return;

    const reason = rejectionReason(file);
    if (reason) {
      setError(reason);
      setFile(null);
      setPhase('idle');
      return;
    }

    setError(null);
    setPercent(0);
    setPhase('uploading');

    try {
      const jobId = await uploadViaServer(file, setPercent);
      setPercent(100);
      setPhase('done');
      window.location.assign(jobId ? '/jobs/' + jobId : '/dashboard');
    } catch (err) {
      setPhase('chosen');
      setPercent(0);
      const message = err instanceof Error ? err.message : String(err);
      setError(
        /row-level security|not authorized|jwt|401|403/i.test(message)
          ? 'Your session has gone stale, so the archive would not accept the deposit. Sign in again and retry — the file itself is fine.'
          : 'The deposit did not go through: ' + message
      );
    }
  }

  return (
    <div className="intake">
      <div
        className={'intake-slot' + (dragging ? ' is-dragging' : '') + (file ? ' has-file' : '') + (busy ? ' is-busy' : '')}
        onDrop={handleDrop}
        onDragOver={(e) => e.preventDefault()}
        onDragEnter={handleDragEnter}
        onDragLeave={handleDragLeave}
      >
        <span className="intake-slot-corner" aria-hidden="true"></span>

        {!file && (
          <>
            <p className="intake-slot-label">Deposit slot</p>
            <p className="intake-slot-head">Place your book here.</p>
            <p className="intake-slot-note">
              Drag a PDF onto the desk, or hand it over directly.
            </p>
            <button type="button" className="intake-browse" onClick={() => inputRef.current?.click()}>
              Choose a PDF
            </button>
            <p className="intake-slot-fine">PDF only · up to 50 MB · one volume at a time</p>
          </>
        )}

        {file && (
          <>
            <p className="intake-slot-label">On the desk</p>
            <dl className="intake-docket">
              <div>
                <dt>Title as filed</dt>
                <dd className="intake-docket-file">{file.name}</dd>
              </div>
              <div>
                <dt>Size</dt>
                <dd>{formatSize(file.size)}</dd>
              </div>
              <div>
                <dt>Format</dt>
                <dd>PDF · accepted</dd>
              </div>
              <div>
                <dt>Rate</dt>
                <dd>{CREDITS_PER_PAGE} credits per page</dd>
              </div>
            </dl>

            {(phase === 'uploading' || phase === 'filing' || phase === 'done') && (
              <div className="intake-progress">
                <div
                  className="intake-progress-track"
                  role="progressbar"
                  aria-valuenow={percent}
                  aria-valuemin={0}
                  aria-valuemax={100}
                  aria-label="Upload progress"
                >
                  <div className="intake-progress-fill" style={{ width: percent + '%' }}></div>
                </div>
                <p className="intake-progress-text" role="status">
                  {phase === 'uploading'
                    ? 'Transferring to the archive · ' + percent + '%'
                    : phase === 'filing'
                      ? 'Writing the register entry...'
                      : 'Accepted. Opening the record...'}
                </p>
              </div>
            )}

            {!busy && (
              <div className="intake-slot-actions">
                <button type="button" className="intake-submit" onClick={handleSubmit}>
                  Deposit for restoration
                </button>
                <button type="button" className="intake-clear" onClick={clearFile}>
                  Take it back
                </button>
              </div>
            )}
          </>
        )}

        <input
          ref={inputRef}
          type="file"
          accept="application/pdf,.pdf"
          className="intake-input"
          onChange={handleChange}
          disabled={busy}
        />
      </div>

      {error && (
        <p className="intake-error" role="alert">
          <span className="intake-error-mark" aria-hidden="true">
            Returned
          </span>
          {error}
        </p>
      )}

      <p className="intake-balance">
        You hold <strong>{credits}</strong> credits · at {CREDITS_PER_PAGE} credits per page that
        covers about <strong>{Math.floor(credits / CREDITS_PER_PAGE)}</strong> pages. Nothing is
        charged until the page count is known and you approve it.
      </p>
    </div>
  );
}
