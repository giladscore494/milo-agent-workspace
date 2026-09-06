'use client';
import { safeText } from '@/lib/sanitize';

/** Shown while the Supabase session is being restored: never a workspace. */
export function SessionRestoreScreen() {
  return (
    <main className="auth-screen">
      <div className="auth-card">
        <p className="eyebrow">MILO agentic workspace</p>
        <p className="muted">Restoring your MILO session…</p>
      </div>
    </main>
  );
}

export type AuthScreenProps = {
  email: string;
  password: string;
  error: string;
  onEmailChange: (value: string) => void;
  onPasswordChange: (value: string) => void;
  onSubmit: () => void;
};

/**
 * Unauthenticated surface. It renders no workspace data at all: the page keeps
 * project, conversation and run state unloaded until a session exists.
 */
export function AuthScreen({ email, password, error, onEmailChange, onPasswordChange, onSubmit }: AuthScreenProps) {
  return (
    <main className="auth-screen">
      <form
        className="auth-card"
        onSubmit={(event) => {
          event.preventDefault();
          onSubmit();
        }}
      >
        <p className="eyebrow">MILO agentic workspace</p>
        <h1 className="auth-title">MILO</h1>
        <p className="muted">Sign in to access the authenticated workspace.</p>
        <div className="field">
          <label className="field-label" htmlFor="auth-email">Email</label>
          <input id="auth-email" type="email" autoComplete="email" value={email} onChange={(event) => onEmailChange(event.target.value)} placeholder="you@example.com" />
        </div>
        <div className="field">
          <label className="field-label" htmlFor="auth-password">Password</label>
          <input id="auth-password" type="password" autoComplete="current-password" value={password} onChange={(event) => onPasswordChange(event.target.value)} placeholder="••••••••" />
        </div>
        <button className="button button--primary button--block" type="submit">Login</button>
        {error && <p className="alert" role="alert">{safeText(error)}</p>}
      </form>
    </main>
  );
}
