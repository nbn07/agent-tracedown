"use client";
import { KeyRound } from "lucide-react";
import { useEffect, useState } from "react";
import { Dialog } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { api, getApiKey, setApiKey } from "@/lib/api";

interface Me { id: string; kind: string; email: string | null; org_role: string | null; open_mode: boolean; sso: boolean }

export function SettingsButton() {
  const [open, setOpen] = useState(false);
  const [key, setKey] = useState("");
  const [me, setMe] = useState<Me | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [hasKey, setHasKey] = useState(false);

  const refresh = () => api<Me>("/me").then(m => { setMe(m); setErr(null); }).catch(e => { setMe(null); setErr(e.status === 401 ? "Not signed in" : "Server unreachable"); });
  useEffect(() => { setHasKey(Boolean(getApiKey())); refresh(); }, []);
  useEffect(() => { if (open) { setKey(getApiKey()); refresh(); } }, [open]);

  const save = () => { setApiKey(key.trim()); setHasKey(Boolean(key.trim())); refresh(); setOpen(false); };

  return (
    <>
      <button onClick={() => setOpen(true)} aria-label="Settings: API key and sign-in"
        className="relative rounded-md p-2 text-muted hover:text-ink hover:bg-sunken cursor-pointer" title="API key">
        <KeyRound size={16} />
        {(err === "Not signed in") && <span className="absolute right-1 top-1 h-2 w-2 rounded-full bg-cause" aria-hidden />}
      </button>
      <Dialog open={open} onOpenChange={setOpen} title="Connect to Tracedown"
        description="Paste an API key, or sign in with single sign-on. A dev server without keys works with neither.">
        <div className="space-y-4">
          <div className="rounded-md bg-sunken px-3 py-2 text-sm">
            {me ? (
              <span>Connected as <b>{me.email || me.id}</b>{me.org_role ? ` (${me.org_role})` : ""}{me.open_mode ? " · open dev mode, no key needed" : ""}</span>
            ) : <span className="text-bad">{err || "Checking…"}</span>}
          </div>
          <label className="flex flex-col gap-1 text-xs font-medium text-muted" htmlFor="apikey">
            API key
            <input id="apikey" type="password" autoComplete="off" value={key} onChange={e => setKey(e.target.value)} placeholder="tdn_…"
              className="h-9 rounded-md border border-line bg-bg px-2.5 font-mono text-sm text-ink" />
          </label>
          <p className="text-xs text-muted">The key is kept in this browser only and sent with each request.</p>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex gap-2">
              {me?.sso !== false && <a className="text-sm text-accent underline underline-offset-2" href="/auth/oidc/login">Sign in with SSO</a>}
            </div>
            <div className="flex gap-2">
              {hasKey && <Button variant="ghost" onClick={() => { setApiKey(""); setKey(""); setHasKey(false); refresh(); }}>Remove key</Button>}
              <Button variant="primary" onClick={save}>Save</Button>
            </div>
          </div>
        </div>
      </Dialog>
    </>
  );
}
