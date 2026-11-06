import type { TeamConnection, TeamConnectionInput } from './api';
import type { McpCatalogEntry } from './mcp-api';

// ---------------------------------------------------------------------------
// Team MCP connections: form helpers shared by the team connections page.
// ---------------------------------------------------------------------------
//
// A team picks servers from the same catalog as the global MCP page, but
// every credential (token, env values, OAuth client secret and sign-in) is
// stored for that team's connection only.

export interface TeamConnectionForm {
  catalog_id: string;
  name: string;
  transport: 'http' | 'stdio';
  url: string;
  auth_type: TeamConnection['auth_type'];
  header_name: string;
  token: string;
  command: string;
  /** One argument per line. */
  args: string;
  env: { name: string; value: string }[];
  oauth_client_id: string;
  oauth_client_secret: string;
  oauth_scopes: string;
  oauth_auth_params: Record<string, string>;
}

export const emptyTeamConnectionForm: TeamConnectionForm = {
  catalog_id: '',
  name: '',
  transport: 'http',
  url: '',
  auth_type: 'bearer',
  header_name: '',
  token: '',
  command: '',
  args: '',
  env: [],
  oauth_client_id: '',
  oauth_client_secret: '',
  oauth_scopes: '',
  oauth_auth_params: {},
};

/** Pre-fill the add-connection form from a catalog entry. */
export function teamFormFromCatalog(entry: McpCatalogEntry): TeamConnectionForm {
  const s = entry.server;
  return {
    ...emptyTeamConnectionForm,
    catalog_id: entry.id,
    name: entry.name,
    transport: s.transport,
    url: s.url || '',
    auth_type: s.transport === 'http' ? s.auth_type : 'none',
    header_name: s.auth_header || '',
    command: s.command || '',
    args: (s.args || []).join('\n'),
    env: (s.env_names || []).map((name) => ({ name, value: '' })),
    oauth_scopes: (s.oauth_scopes || []).join(' '),
    oauth_auth_params: { ...(s.oauth_auth_params || {}) },
  };
}

/** Build the create payload; only fields of the chosen transport and auth. */
export function teamConnectionPayload(form: TeamConnectionForm, agentIds: string[] = []): TeamConnectionInput {
  const name = form.name.trim();
  const settings: NonNullable<TeamConnectionInput['settings']> = { agent_ids: agentIds };
  if (form.catalog_id) settings.catalog_id = form.catalog_id;
  if (form.transport === 'stdio') {
    const rows = form.env.map((r) => ({ name: r.name.trim(), value: r.value })).filter((r) => r.name);
    settings.command = form.command.trim();
    settings.args = form.args.split('\n').map((a) => a.trim()).filter(Boolean);
    settings.env_names = rows.map((r) => r.name);
    const env = Object.fromEntries(rows.filter((r) => r.value).map((r) => [r.name, r.value]));
    return { name, provider: name, transport: 'stdio', auth_type: 'none', settings, ...(Object.keys(env).length ? { env } : {}) };
  }
  const input: TeamConnectionInput = { name, provider: name, transport: 'http', url: form.url.trim(), auth_type: form.auth_type, settings };
  if (form.auth_type === 'header') settings.header_name = form.header_name.trim();
  if (form.auth_type === 'bearer' || form.auth_type === 'header') input.token = form.token;
  if (form.auth_type === 'oauth') {
    if (form.oauth_client_id.trim()) settings.oauth_client_id = form.oauth_client_id.trim();
    settings.oauth_scopes = form.oauth_scopes.split(/[\s,]+/).filter(Boolean);
    settings.oauth_auth_params = form.oauth_auth_params;
    if (form.oauth_client_secret) input.oauth_client_secret = form.oauth_client_secret;
  }
  return input;
}

/** What the card shows as the server's address. */
export function connectionTarget(connection: TeamConnection): string {
  if (connection.transport === 'stdio') {
    const s = connection.settings || {};
    return [s.command, ...(s.args || [])].filter(Boolean).join(' ');
  }
  return connection.url;
}
