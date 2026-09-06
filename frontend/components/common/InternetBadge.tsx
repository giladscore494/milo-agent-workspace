'use client';
import { safeText } from '@/lib/sanitize';
import { InternetPolicy } from '@/lib/types';

/**
 * Every internet policy the backend can report.
 *
 * Each policy renders through its own scoped `is-policy-*` state class, so a
 * selected navigation item can never accidentally inherit an execution-state
 * colour from a shared global class.
 */
export const INTERNET_POLICIES: InternetPolicy[] = [
  'forbidden',
  'allowed',
  'required',
  'conditional',
  'requested',
  'approved',
  'denied',
  'active',
];

export function InternetBadge({ policy, reason }: { policy: InternetPolicy; reason?: string }) {
  return (
    <span className={`badge badge--policy is-policy-${policy}`}>
      {policy} internet — {safeText(reason || 'policy visible')}
    </span>
  );
}
