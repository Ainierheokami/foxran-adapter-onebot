import { host } from '../host'

// Served by this adapter's own routes (/api/adapters/onebot_v11/...).
const base = '/adapters/onebot_v11'

export async function loadAccounts() {
  return (await host.api.get(`${base}/accounts`)).data.accounts
}

export async function saveAccounts(accounts: unknown[]) {
  return (await host.api.put(`${base}/accounts`, { accounts })).data.accounts
}

export async function reloadAccounts() {
  return (await host.api.post(`${base}/reload`)).data.accounts
}
