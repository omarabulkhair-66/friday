import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import type { AgentTeam, TeamConnection } from '../lib/api';
import { connectionTarget, emptyTeamConnectionForm, teamConnectionPayload, teamFormFromCatalog } from '../lib/team-mcp';
import { MCP_CATALOG } from './mcp/McpCatalog';
import { AddConnection, ConnectionCard } from './TeamConnections';

const team: AgentTeam = { id: 'team-a', name: 'سوفت باك', created_at: 0 } as AgentTeam;
const gmail = MCP_CATALOG.find((e) => e.id === 'gmail')!;

function oauthConnection(patch: Partial<TeamConnection> = {}): TeamConnection {
  return {
    id: 'c1', team_id: 'team-a', name: 'Gmail', provider: 'Gmail', transport: 'http',
    url: 'https://gmailmcp.googleapis.com/mcp/v1', auth_type: 'oauth', settings: { agent_ids: [] },
    env_set: [], has_client_secret: true, oauth_status: 'signed_out', status: 'needs_authentication', ...patch,
  };
}

describe('team connections from the MCP library', () => {
  it('pre-fills Gmail from the same catalog with OAuth sign-in', () => {
    const form = teamFormFromCatalog(gmail);
    expect(form).toMatchObject({ catalog_id: 'gmail', name: 'Gmail', transport: 'http', auth_type: 'oauth', url: gmail.server.url });
    expect(form.oauth_scopes).toContain('gmail.readonly');
    const payload = teamConnectionPayload({ ...form, oauth_client_id: ' cid ', oauth_client_secret: 'test-secret' });
    expect(payload).toMatchObject({ transport: 'http', auth_type: 'oauth', oauth_client_secret: 'test-secret' });
    expect(payload.settings).toMatchObject({ catalog_id: 'gmail', oauth_client_id: 'cid', oauth_auth_params: { access_type: 'offline', prompt: 'consent' }, agent_ids: [] });
    expect(payload.token).toBeUndefined();
  });

  it('builds a stdio payload with env values kept out of settings', () => {
    const payload = teamConnectionPayload({
      ...emptyTeamConnectionForm, name: ' Files ', transport: 'stdio', command: ' npx ', args: '-y\n server \n',
      env: [{ name: 'API_KEY', value: 'test-value' }, { name: 'EMPTY', value: '' }, { name: ' ', value: 'x' }],
    }, ['agent-1']);
    expect(payload).toEqual({
      name: 'Files', provider: 'Files', transport: 'stdio', auth_type: 'none',
      settings: { agent_ids: ['agent-1'], command: 'npx', args: ['-y', 'server'], env_names: ['API_KEY', 'EMPTY'] },
      env: { API_KEY: 'test-value' },
    });
    expect(JSON.stringify(payload.settings)).not.toContain('test-value');
  });

  it('sends a token only for token auth', () => {
    const base = { ...emptyTeamConnectionForm, name: 'x', url: 'https://x.test/mcp', token: 'test-token' };
    expect(teamConnectionPayload({ ...base, auth_type: 'none' }).token).toBeUndefined();
    expect(teamConnectionPayload({ ...base, auth_type: 'header', header_name: 'X-Key' })).toMatchObject({ token: 'test-token', settings: { header_name: 'X-Key' } });
  });

  it('shows the OAuth setup steps and client fields for a catalog entry', () => {
    const html = renderToStaticMarkup(<AddConnection team={team} initial={teamFormFromCatalog(gmail)} entry={gmail} onAdded={vi.fn()} onCancel={vi.fn()} />);
    expect(html).toContain('خطوات إنشاء عميل OAuth');
    expect(html).toContain('Client ID');
    expect(html).toContain('حفظ الاتصال');
    expect(html).not.toContain('مفتاح الوصول');
  });

  it('offers per-team sign-in and blocks testing until signed in', () => {
    const html = renderToStaticMarkup(<ConnectionCard team={team} connection={oauthConnection()} members={[]} onVerify={vi.fn()} onSignIn={vi.fn()} onChanged={vi.fn()} />);
    expect(html).toContain('يحتاج تسجيل دخول');
    expect(html).toContain('>تسجيل الدخول<');
    expect(html).toMatch(/disabled="" class="underline disabled:opacity-40">اختبار الاتصال/);
    const signedIn = renderToStaticMarkup(<ConnectionCard team={team} connection={oauthConnection({ oauth_status: 'connected', status: 'verified' })} members={[]} onVerify={vi.fn()} onSignIn={vi.fn()} onChanged={vi.fn()} />);
    expect(signedIn).toContain('تسجيل الخروج');
    expect(signedIn).toContain('متحقق منه');
  });

  it('shows a stdio command as the target', () => {
    const c = oauthConnection({ transport: 'stdio', auth_type: 'none', url: '', oauth_status: null, settings: { command: 'npx', args: ['-y', 'pkg'] } });
    expect(connectionTarget(c)).toBe('npx -y pkg');
  });
});
