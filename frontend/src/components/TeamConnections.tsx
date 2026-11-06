import { useCallback, useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';
import {
  createTeamConnection, deleteTeamConnection, fetchTeamConnections,
  setTeamConnectionAccess, signOutTeamConnectionOAuth, startTeamConnectionOAuth, verifyTeamConnection,
} from '../lib/api';
import type { AgentTeam, ManagedAgent, TeamConnection } from '../lib/api';
import { getMcpOAuthRedirectUri, type McpCatalogEntry } from '../lib/mcp-api';
import {
  connectionTarget, emptyTeamConnectionForm, teamConnectionPayload, teamFormFromCatalog,
  type TeamConnectionForm,
} from '../lib/team-mcp';
import { McpCatalog } from './mcp/McpCatalog';

const field = 'rounded-lg px-3 py-2 text-sm w-full disabled:opacity-40';
const style = { background: 'var(--color-bg-secondary)', color: 'var(--color-text)', border: '1px solid var(--color-border)' };
const muted = { color: 'var(--color-text-secondary)' };
const card = { border: '1px solid var(--color-border)', borderRadius: 12 };

const STATUS: Record<TeamConnection['status'], { label: string; tone: 'ok' | 'warn' | 'bad' }> = {
  verified: { label: 'متحقق منه', tone: 'ok' },
  configured: { label: 'محفوظ — لم يُختبر بعد', tone: 'warn' },
  error: { label: 'فشل الاختبار', tone: 'bad' },
  needs_authentication: { label: 'يحتاج مفتاحًا', tone: 'bad' },
};

function statusLabel(connection: TeamConnection): string {
  if (connection.status !== 'needs_authentication') return STATUS[connection.status].label;
  if (connection.auth_type === 'oauth') return connection.oauth_status === 'expired' ? 'انتهى تسجيل الدخول' : 'يحتاج تسجيل دخول';
  if (connection.transport === 'stdio') return 'ينقصه متغيرات بيئة';
  return STATUS.needs_authentication.label;
}

function StatusChip({ connection }: { connection: TeamConnection }) {
  const tone = STATUS[connection.status].tone;
  const color = tone === 'ok' ? 'var(--color-success, #1f7a4d)' : tone === 'bad' ? 'var(--color-error, #b3261e)' : 'var(--color-text-secondary)';
  return <span className="text-xs font-medium px-2 py-0.5 rounded-full" style={{ border: `1px solid ${color}`, color }}>{statusLabel(connection)}</span>;
}

export function AddConnection({ team, initial, entry, onAdded, onCancel }: {
  team: AgentTeam; initial: TeamConnectionForm; entry?: McpCatalogEntry;
  onAdded: (c: TeamConnection) => void; onCancel: () => void;
}) {
  const [form, setForm] = useState<TeamConnectionForm>(initial);
  const [busy, setBusy] = useState(false);
  const [redirectUri, setRedirectUri] = useState('');
  const set = <K extends keyof TeamConnectionForm>(key: K, value: TeamConnectionForm[K]) => setForm((f) => ({ ...f, [key]: value }));
  const setEnv = (index: number, patch: Partial<TeamConnectionForm['env'][number]>) =>
    setForm((f) => ({ ...f, env: f.env.map((row, i) => (i === index ? { ...row, ...patch } : row)) }));
  const oauth = form.transport === 'http' && form.auth_type === 'oauth';
  useEffect(() => {
    if (oauth && !redirectUri) getMcpOAuthRedirectUri().then((r) => setRedirectUri(r.redirect_uri)).catch(() => {});
  }, [oauth, redirectUri]);
  async function save(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      const created = await createTeamConnection(team.id, teamConnectionPayload(form));
      setForm((f) => ({ ...f, token: '', oauth_client_secret: '', env: f.env.map((r) => ({ ...r, value: '' })) }));
      onAdded(created);
    } catch (error) { toast.error(error instanceof Error ? error.message : 'تعذّر حفظ الاتصال.'); }
    finally { setBusy(false); }
  }
  return <form onSubmit={save} className="space-y-3 max-w-xl p-4" style={card} aria-label="إضافة اتصال">
    <p className="text-sm font-semibold">إضافة {entry ? <span dir="auto">{entry.name}</span> : 'خادم MCP'} لفريق {team.name}</p>
    <label className="block text-sm">اسم الاتصال<input required maxLength={120} dir="auto" className={`${field} mt-1`} style={style} value={form.name} onChange={(e) => set('name', e.target.value)} disabled={busy} placeholder="مثال: Gmail المبيعات" /></label>
    <label className="block text-sm">نوع الخادم<select className={`${field} mt-1`} style={style} value={form.transport} onChange={(e) => set('transport', e.target.value as TeamConnectionForm['transport'])} disabled={busy}>
      <option value="http">خادم بعيد (Streamable HTTP)</option>
      <option value="stdio">أمر محلي (stdio)</option>
    </select></label>
    {form.transport === 'http' ? <>
      <label className="block text-sm">عنوان خادم MCP<input required type="url" dir="ltr" className={`${field} mt-1`} style={style} value={form.url} onChange={(e) => set('url', e.target.value)} disabled={busy} placeholder="https://…/mcp" /></label>
      <label className="block text-sm">طريقة المصادقة<select className={`${field} mt-1`} style={style} value={form.auth_type} onChange={(e) => set('auth_type', e.target.value as TeamConnectionForm['auth_type'])} disabled={busy}>
        <option value="oauth">تسجيل الدخول (OAuth)</option>
        <option value="bearer">مفتاح Bearer (Authorization)</option>
        <option value="header">مفتاح في ترويسة مخصصة</option>
        <option value="none">بدون مصادقة</option>
      </select></label>
      {form.auth_type === 'header' && <label className="block text-sm">اسم الترويسة<input required dir="ltr" className={`${field} mt-1`} style={style} value={form.header_name} onChange={(e) => set('header_name', e.target.value)} disabled={busy} placeholder="X-API-Key" /></label>}
      {(form.auth_type === 'bearer' || form.auth_type === 'header') && <label className="block text-sm">مفتاح الوصول<input type="password" autoComplete="off" dir="ltr" className={`${field} mt-1`} style={style} value={form.token} onChange={(e) => set('token', e.target.value)} disabled={busy} /></label>}
      {oauth && <>
        {entry?.oauth_setup && entry.oauth_setup.length > 0 && <details className="text-sm p-3" style={card}>
          <summary className="font-medium">خطوات إنشاء عميل OAuth (من مكتبة MCP)</summary>
          <ol dir="ltr" className="list-decimal ps-5 mt-2 space-y-1 text-xs" style={muted}>{entry.oauth_setup.map((step) => <li key={step}>{step}</li>)}</ol>
        </details>}
        {redirectUri && <div className="text-sm">رابط إعادة التوجيه (Redirect URI)<code dir="ltr" className="block mt-1 px-2 py-1.5 rounded text-xs break-all select-all" style={style}>{redirectUri}</code></div>}
        <label className="block text-sm">معرّف العميل (Client ID)<input dir="ltr" className={`${field} mt-1`} style={style} value={form.oauth_client_id} onChange={(e) => set('oauth_client_id', e.target.value)} disabled={busy} /></label>
        <span className="block text-xs" style={muted}>اتركه فارغًا إن كان الخادم يسجّل العملاء تلقائيًا.</span>
        <label className="block text-sm">سر العميل (Client secret)<input type="password" autoComplete="off" dir="ltr" className={`${field} mt-1`} style={style} value={form.oauth_client_secret} onChange={(e) => set('oauth_client_secret', e.target.value)} disabled={busy} /></label>
        <label className="block text-sm">الصلاحيات (Scopes)<input dir="ltr" className={`${field} mt-1 font-mono`} style={style} value={form.oauth_scopes} onChange={(e) => set('oauth_scopes', e.target.value)} disabled={busy} /></label>
        <p className="text-xs" style={muted}>احفظ الاتصال ثم اضغط «تسجيل الدخول» واختر حساب هذه الشركة.</p>
      </>}
    </> : <>
      <label className="block text-sm">الأمر<input required dir="ltr" className={`${field} mt-1 font-mono`} style={style} value={form.command} onChange={(e) => set('command', e.target.value)} disabled={busy} placeholder="npx" /></label>
      <label className="block text-sm">الوسائط (سطر لكل وسيط)<textarea dir="ltr" rows={3} className={`${field} mt-1 font-mono`} style={style} value={form.args} onChange={(e) => set('args', e.target.value)} disabled={busy} /></label>
      <span className="block text-xs" style={muted}>لا تضع مفاتيح في الوسائط؛ تُحفظ كنص عادي. ضعها في متغيرات البيئة.</span>
      <fieldset className="space-y-1 text-sm">
        <legend>متغيرات البيئة السرية</legend>
        {form.env.map((row, i) => <div key={i} className="flex gap-2">
          <input dir="ltr" aria-label="اسم المتغير" className={`${field} font-mono`} style={{ ...style, maxWidth: '40%' }} value={row.name} onChange={(e) => setEnv(i, { name: e.target.value })} disabled={busy} placeholder="API_KEY" />
          <input type="password" autoComplete="off" dir="ltr" aria-label={`قيمة ${row.name || 'المتغير'}`} className={field} style={style} value={row.value} onChange={(e) => setEnv(i, { value: e.target.value })} disabled={busy} />
          <button type="button" aria-label={`حذف ${row.name || 'المتغير'}`} className="underline" onClick={() => set('env', form.env.filter((_, j) => j !== i))} disabled={busy}>×</button>
        </div>)}
        <button type="button" className="underline text-sm" onClick={() => set('env', [...form.env, { name: '', value: '' }])} disabled={busy}>+ متغير</button>
      </fieldset>
      <p className="text-xs" style={muted}>يعمل الأمر على هذا الجهاز بمتغيرات هذا الفريق فقط، دون مفاتيح بيئة الخادم.</p>
    </>}
    <p className="text-xs" style={muted}>استخدم حسابًا ومفاتيح خاصة بهذه الشركة، لا حسابك العام. تُحفظ المفاتيح وتسجيلات الدخول لهذا الفريق فقط ولا تُعرض مرة أخرى. لا يُستخدم أي حساب عام بديلًا عند الفشل.</p>
    <div className="flex gap-4">
      <button type="submit" disabled={busy} className="text-sm underline font-semibold disabled:opacity-40">{busy ? 'جارٍ الحفظ…' : oauth ? 'حفظ الاتصال' : 'حفظ واختبار الاتصال'}</button>
      <button type="button" disabled={busy} onClick={onCancel} className="text-sm underline">إلغاء</button>
    </div>
  </form>;
}

export function ConnectionCard({ team, connection, members, onVerify, onSignIn, onChanged }: {
  team: AgentTeam; connection: TeamConnection; members: ManagedAgent[];
  onVerify: (c: TeamConnection) => Promise<void>; onSignIn: (c: TeamConnection) => void; onChanged: () => Promise<void>;
}) {
  const settings = connection.settings || {};
  const allowed = settings.agent_ids || [];
  const tools = settings.tools || [];
  const [scope, setScope] = useState<'all' | 'some'>(allowed.length ? 'some' : 'all');
  const [picked, setPicked] = useState<string[]>(allowed);
  const [busy, setBusy] = useState(false);
  async function run(action: () => Promise<unknown>, success?: string) {
    setBusy(true);
    try { await action(); if (success) toast.success(success); await onChanged(); }
    catch (error) { toast.error(error instanceof Error ? error.message : 'تعذّر التنفيذ.'); }
    finally { setBusy(false); }
  }
  const dirty = scope === 'all' ? allowed.length > 0 : picked.slice().sort().join() !== allowed.slice().sort().join();
  const oauth = connection.auth_type === 'oauth';
  const signedIn = connection.oauth_status === 'connected';
  return <li className="p-4 space-y-3" style={card}>
    <div className="flex flex-wrap items-center gap-2 justify-between">
      <div className="min-w-0">
        <p className="text-sm font-semibold" dir="auto">{connection.name}</p>
        <p className="text-xs break-all" style={muted}>{connection.transport === 'stdio' ? 'أمر محلي' : 'MCP'}{oauth ? ' · OAuth' : ''} · <span dir="ltr">{connectionTarget(connection)}</span></p>
      </div>
      <StatusChip connection={connection} />
    </div>
    {settings.last_error && <p role="alert" className="text-sm">{settings.last_error}</p>}
    {oauth && <div className="flex flex-wrap items-center gap-3 text-sm">
      <span style={muted}>{signedIn ? 'تم تسجيل الدخول لهذا الفريق.' : 'لم يُسجَّل الدخول بعد لهذا الفريق.'}</span>
      {signedIn
        ? <button type="button" disabled={busy} className="underline disabled:opacity-40" onClick={() => run(() => signOutTeamConnectionOAuth(team.id, connection.id), 'سُجّل الخروج.')}>تسجيل الخروج</button>
        : <button type="button" disabled={busy} className="underline font-semibold disabled:opacity-40" onClick={() => onSignIn(connection)}>{connection.oauth_status === 'expired' ? 'تسجيل الدخول من جديد' : 'تسجيل الدخول'}</button>}
    </div>}
    {tools.length > 0 ? <details className="text-sm"><summary>الأدوات المتاحة ({tools.length})</summary>
      <ul className="mt-2 space-y-1">{tools.map((t) => <li key={t.name}><span className="font-medium" dir="ltr">{t.name}</span> <span className="text-xs" style={muted} dir="auto">{t.description}</span></li>)}</ul>
    </details> : <p className="text-xs" style={muted}>اختبر الاتصال لعرض الأدوات التي يوفرها.</p>}
    <fieldset className="space-y-2 text-sm">
      <legend className="font-medium">من يستخدم هذا الاتصال؟</legend>
      <label className="flex items-center gap-2"><input type="radio" checked={scope === 'all'} onChange={() => setScope('all')} disabled={busy} />كل وكلاء الفريق</label>
      <label className="flex items-center gap-2"><input type="radio" checked={scope === 'some'} onChange={() => setScope('some')} disabled={busy} />وكلاء محددون فقط</label>
      {scope === 'some' && <div className="ps-6 space-y-1">{members.map((m) => <label key={m.id} className="flex items-center gap-2"><input type="checkbox" checked={picked.includes(m.id)} disabled={busy} onChange={(e) => setPicked((c) => e.target.checked ? [...c, m.id] : c.filter((x) => x !== m.id))} /><span dir="auto">{m.name}</span></label>)}</div>}
      {dirty && <button type="button" disabled={busy || (scope === 'some' && picked.length === 0)} className="underline disabled:opacity-40" onClick={() => run(() => setTeamConnectionAccess(team.id, connection.id, scope === 'all' ? [] : picked), 'حُفظت الصلاحيات.')}>حفظ الصلاحيات</button>}
    </fieldset>
    <div className="flex flex-wrap gap-4 text-sm">
      <button type="button" disabled={busy || (oauth && !signedIn)} className="underline disabled:opacity-40" onClick={() => run(() => onVerify(connection))}>اختبار الاتصال</button>
      <button type="button" disabled={busy} className="underline disabled:opacity-40" onClick={() => window.confirm(`حذف «${connection.name}» من ${team.name}؟ ستُحذف مفاتيحه وتسجيل دخوله المحفوظ، ولن تتأثر الفرق الأخرى.`) && run(() => deleteTeamConnection(team.id, connection.id), 'حُذف الاتصال.')}>حذف</button>
    </div>
    <p className="text-xs" style={muted}>لتغيير المفتاح أو العميل احذف الاتصال وأضفه من جديد.</p>
  </li>;
}

type Adding = null | { step: 'catalog' } | { step: 'form'; form: TeamConnectionForm; entry?: McpCatalogEntry };

export function TeamConnections({ team, agents }: { team: AgentTeam; agents: ManagedAgent[] }) {
  const members = agents.filter((a) => a.team_id === team.id && a.status !== 'archived');
  const [connections, setConnections] = useState<TeamConnection[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [adding, setAdding] = useState<Adding>(null);
  const load = useCallback(async () => {
    try { const list = await fetchTeamConnections(team.id); setConnections(list); setError(''); return list; }
    catch { setError('تعذّر تحميل الاتصالات.'); return null; }
    finally { setLoading(false); }
  }, [team.id]);
  useEffect(() => { load(); }, [load]);
  const verify = useCallback(async (connection: TeamConnection) => {
    try {
      await verifyTeamConnection(team.id, connection.id);
      toast.success(`نجح اختبار «${connection.name}».`);
    } finally { await load(); }
  }, [team.id, load]);

  // The sign-in window reports back (or is closed); then re-read and test.
  const signingIn = useRef<{ id: string; popup: Window } | null>(null);
  const finishSignIn = useCallback(async () => {
    const current = signingIn.current;
    if (!current) return;
    signingIn.current = null;
    const list = await load();
    const connection = list?.find((c) => c.id === current.id);
    if (connection?.oauth_status === 'connected') {
      try { await verify(connection); } catch (e) { toast.error(e instanceof Error ? e.message : 'فشل اختبار الاتصال.'); }
    }
  }, [load, verify]);
  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      if (event.data?.type === 'openjarvis-mcp-oauth') void finishSignIn();
    };
    window.addEventListener('message', onMessage);
    const timer = window.setInterval(() => { if (signingIn.current?.popup.closed) void finishSignIn(); }, 1000);
    return () => { window.removeEventListener('message', onMessage); window.clearInterval(timer); };
  }, [finishSignIn]);
  async function signIn(connection: TeamConnection) {
    // Open the window during the click so it is not blocked as a popup.
    const popup = window.open('', 'openjarvis-mcp-oauth', 'width=520,height=700');
    try {
      const { authorization_url } = await startTeamConnectionOAuth(team.id, connection.id);
      if (!popup) { window.location.assign(authorization_url); return; }
      popup.location.href = authorization_url;
      signingIn.current = { id: connection.id, popup };
    } catch (e) {
      popup?.close();
      toast.error(e instanceof Error ? e.message : 'تعذّر بدء تسجيل الدخول.');
    }
  }

  return <section aria-labelledby="team-connections-title" className="space-y-3">
    <div>
      <h3 id="team-connections-title" className="font-semibold text-sm">اتصالات الفريق</h3>
      <p className="text-sm max-w-2xl" style={muted}>حسابات هذه الشركة فقط. اختر خادمًا من مكتبة MCP نفسها أو أضف خادمًا مخصصًا؛ تظهر أدواته للوكلاء المسموح لهم فقط. لا يُستخدم أي حساب عام بديلًا عند الفشل.</p>
    </div>
    {loading ? <p role="status" className="text-sm">جارٍ التحميل…</p> : error ? <p role="alert" className="text-sm">{error} <button type="button" onClick={load} className="underline">إعادة المحاولة</button></p> : <>
      {connections.length === 0 && !adding && <p className="text-sm">لا توجد اتصالات بعد.</p>}
      <ul className="space-y-3">{connections.map((c) => <ConnectionCard key={c.id} team={team} connection={c} members={members} onVerify={verify} onSignIn={signIn} onChanged={async () => { await load(); }} />)}</ul>
    </>}
    {adding?.step === 'catalog' && <div className="p-4" style={card} dir="ltr">
      <McpCatalog
        onPick={(entry) => setAdding({ step: 'form', form: teamFormFromCatalog(entry), entry })}
        onCustom={() => setAdding({ step: 'form', form: emptyTeamConnectionForm })}
      />
      <div dir="rtl" className="mt-3"><button type="button" onClick={() => setAdding(null)} className="text-sm underline">إلغاء</button></div>
    </div>}
    {adding?.step === 'form' && <AddConnection team={team} initial={adding.form} entry={adding.entry} onCancel={() => setAdding(null)} onAdded={async (created) => {
      setAdding(null); await load();
      if (created.auth_type === 'oauth') { toast.success('حُفظ الاتصال. سجّل الدخول الآن بحساب هذه الشركة.'); return; }
      if (created.status === 'needs_authentication') return;
      try { await verify(created); } catch (e) { toast.error(e instanceof Error ? e.message : 'فشل اختبار الاتصال.'); }
    }} />}
    {!adding && <button type="button" onClick={() => setAdding({ step: 'catalog' })} className="text-sm underline font-semibold">+ إضافة اتصال</button>}
  </section>;
}
