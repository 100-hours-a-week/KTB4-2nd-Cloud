export function requiredEnv(name) {
  const value = __ENV[name];
  if (!value || !value.trim()) {
    throw new Error(`${name} 환경변수가 필요합니다.`);
  }
  return value.trim();
}

export function optionalEnv(name, fallback = '') {
  const value = __ENV[name];
  return value && value.trim() ? value.trim() : fallback;
}

export function positiveIntegerEnv(name, fallback) {
  const raw = optionalEnv(name, String(fallback));
  const value = Number(raw);
  if (!Number.isInteger(value) || value < 1) {
    throw new Error(`${name}은 1 이상의 정수여야 합니다.`);
  }
  return value;
}

export function normalizeBaseUrl(value) {
  const url = value.replace(/\/+$/, '');
  if (!url.startsWith('https://')) {
    throw new Error('K6_BASE_URL은 운영 HTTPS URL이어야 합니다.');
  }
  return url;
}

export function csvEnv(name, fallback) {
  const values = optionalEnv(name, fallback)
    .split(',')
    .map((value) => value.trim())
    .filter(Boolean);
  if (values.length === 0) {
    throw new Error(`${name}에는 하나 이상의 값이 필요합니다.`);
  }
  return values;
}

export function safeTripName(prefix = '부하') {
  const suffix = Date.now().toString().slice(-6);
  const maxPrefixLength = 10 - suffix.length;
  const normalized = Array.from(prefix).slice(0, maxPrefixLength).join('');
  return `${normalized || '부하'}${suffix}`;
}

export function utcDateOffset(days) {
  const date = new Date();
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
}
