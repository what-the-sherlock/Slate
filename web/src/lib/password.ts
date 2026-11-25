/**
 * Live hints that mirror core/domain/auth.py validate_password (length only, per NIST
 * SP 800-63B). The server stays the authority; these only save a round trip.
 */
export const PASSWORD_RULES: { label: string; test: (pw: string) => boolean }[] = [
  { label: "At least 12 characters", test: (pw) => pw.length >= 12 && pw.length <= 256 },
  { label: "No spaces at the start or end", test: (pw) => pw.length > 0 && pw.trim() === pw },
];

export const passwordOk = (pw: string) => PASSWORD_RULES.every((r) => r.test(pw));

export const EMAIL = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

/** A 6-digit one-time code, allowing the spaces people paste with it. */
export const normaliseCode = (raw: string) => raw.replace(/\s+/g, "");
export const CODE = /^\d{6}$/;
