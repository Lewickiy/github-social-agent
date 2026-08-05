/**
 * Curated list of common IANA timezones for the manual selector on the
 * Management page, with live UTC-offset labels computed in the browser
 * (so DST-affected zones show their current offset).
 */

const RAW_TIMEZONES = [
  // UTC
  "UTC",
  // Americas
  "America/Honolulu",
  "America/Anchorage",
  "America/Los_Angeles",
  "America/Denver",
  "America/Chicago",
  "America/Mexico_City",
  "America/New_York",
  "America/Toronto",
  "America/Bogota",
  "America/Caracas",
  "America/Santiago",
  "America/Sao_Paulo",
  "America/Buenos_Aires",
  // Europe / Africa
  "Europe/London",
  "Europe/Dublin",
  "Europe/Lisbon",
  "Europe/Madrid",
  "Europe/Paris",
  "Europe/Berlin",
  "Europe/Amsterdam",
  "Europe/Brussels",
  "Europe/Rome",
  "Europe/Zurich",
  "Europe/Stockholm",
  "Europe/Vienna",
  "Europe/Warsaw",
  "Europe/Prague",
  "Africa/Casablanca",
  "Africa/Lagos",
  "Africa/Cairo",
  "Europe/Athens",
  "Europe/Helsinki",
  "Europe/Bucharest",
  "Europe/Sofia",
  "Africa/Johannesburg",
  "Europe/Istanbul",
  "Europe/Moscow",
  "Europe/Kyiv",
  "Europe/Minsk",
  "Africa/Nairobi",
  // Middle East / Asia
  "Asia/Baghdad",
  "Asia/Riyadh",
  "Asia/Tehran",
  "Asia/Dubai",
  "Asia/Baku",
  "Asia/Kabul",
  "Asia/Karachi",
  "Asia/Yekaterinburg",
  "Asia/Kolkata",
  "Asia/Kathmandu",
  "Asia/Dhaka",
  "Asia/Yangon",
  "Asia/Bangkok",
  "Asia/Jakarta",
  "Asia/Ho_Chi_Minh",
  "Asia/Singapore",
  "Asia/Hong_Kong",
  "Asia/Shanghai",
  "Asia/Taipei",
  "Asia/Manila",
  "Asia/Kuala_Lumpur",
  "Asia/Seoul",
  "Asia/Tokyo",
  // Australia / Pacific
  "Australia/Perth",
  "Australia/Adelaide",
  "Australia/Darwin",
  "Australia/Brisbane",
  "Australia/Sydney",
  "Australia/Melbourne",
  "Pacific/Auckland",
  "Pacific/Fiji",
  "Pacific/Apia",
];

/** Current UTC offset (minutes) of *tz*, or null if it cannot be resolved. */
function offsetMinutes(tz: string): number | null {
  try {
    const now = new Date();
    const utc = new Date(now.toLocaleString("en-US", { timeZone: "UTC" }));
    const local = new Date(now.toLocaleString("en-US", { timeZone: tz }));
    return Math.round((local.getTime() - utc.getTime()) / 60000);
  } catch {
    return null;
  }
}

/** Format an offset in minutes as e.g. "+03", "-04:30", "00". */
export function formatOffset(min: number): string {
  if (min === 0) return "00";
  const sign = min < 0 ? "−" : "+";
  const abs = Math.abs(min);
  const h = Math.floor(abs / 60);
  const m = abs % 60;
  return `${sign}${String(h).padStart(2, "0")}${m ? `:${String(m).padStart(2, "0")}` : ""}`;
}

export interface TimezoneOption {
  value: string;
  label: string;
}

export const TIMEZONE_OPTIONS: TimezoneOption[] = RAW_TIMEZONES
  .map((value) => {
    const min = offsetMinutes(value);
    return {
      value,
      offset: min,
      label: `${value.replace(/_/g, " ")} (UTC${
        min === null ? "" : formatOffset(min)
      })`,
    };
  })
  .sort(
    (a, b) => (a.offset ?? 0) - (b.offset ?? 0) || a.value.localeCompare(b.value)
  )
  .map(({ value, label }) => ({ value, label }));

/** IANA timezone reported by the browser, or null when unavailable. */
export function detectSystemTimezone(): string | null {
  try {
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    return tz && tz !== "Etc/Unknown" ? tz : null;
  } catch {
    return null;
  }
}
