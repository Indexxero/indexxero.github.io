/**
 * A zip writer, store-only, about sixty lines.
 *
 * WHY NOT A LIBRARY. One more dependency on a page whose claim is that it sends
 * nothing anywhere is one more thing a reader has to audit to believe the claim.
 * This is short enough to read in a minute.
 *
 * WHY STORED AND NOT DEFLATED. `CompressionStream('deflate-raw')` is not in
 * every Safari this will meet, and a zip that fails to open on a partner's
 * laptop costs more than the bytes. The thirteen files are small; the scale
 * shape is a few MB.
 */
const enc = new TextEncoder();

const CRC = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) { let c = n; for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1; t[n] = c >>> 0; }
  return (buf) => { let c = 0xffffffff; for (let i = 0; i < buf.length; i++) c = t[(c ^ buf[i]) & 0xff] ^ (c >>> 8); return (c ^ 0xffffffff) >>> 0; };
})();

export function makeZip(files) {
  const parts = [], central = [];
  let offset = 0;
  // A fixed DOS timestamp, deliberately: the zip is a container and its clock
  // is not a finding. The run's own time is in receipt.json, hashed.
  const time = 0, date = ((2026 - 1980) << 9) | (1 << 5) | 1;

  for (const [name, data] of Object.entries(files)) {
    const nameBytes = enc.encode(name);
    const body = data instanceof Uint8Array ? data : enc.encode(String(data));
    const crc = CRC(body);
    const local = new DataView(new ArrayBuffer(30));
    local.setUint32(0, 0x04034b50, true); local.setUint16(4, 20, true); local.setUint16(6, 0, true);
    local.setUint16(8, 0, true); local.setUint16(10, time, true); local.setUint16(12, date, true);
    local.setUint32(14, crc, true); local.setUint32(18, body.length, true); local.setUint32(22, body.length, true);
    local.setUint16(26, nameBytes.length, true); local.setUint16(28, 0, true);
    parts.push(new Uint8Array(local.buffer), nameBytes, body);

    const cen = new DataView(new ArrayBuffer(46));
    cen.setUint32(0, 0x02014b50, true); cen.setUint16(4, 20, true); cen.setUint16(6, 20, true);
    cen.setUint16(8, 0, true); cen.setUint16(10, 0, true); cen.setUint16(12, time, true); cen.setUint16(14, date, true);
    cen.setUint32(16, crc, true); cen.setUint32(20, body.length, true); cen.setUint32(24, body.length, true);
    cen.setUint16(28, nameBytes.length, true); cen.setUint16(30, 0, true); cen.setUint16(32, 0, true);
    cen.setUint16(34, 0, true); cen.setUint16(36, 0, true); cen.setUint32(38, 0, true); cen.setUint32(42, offset, true);
    central.push(new Uint8Array(cen.buffer), nameBytes);
    offset += 30 + nameBytes.length + body.length;
  }

  const centralSize = central.reduce((n, p) => n + p.length, 0);
  const end = new DataView(new ArrayBuffer(22));
  end.setUint32(0, 0x06054b50, true); end.setUint16(8, Object.keys(files).length, true);
  end.setUint16(10, Object.keys(files).length, true); end.setUint32(12, centralSize, true);
  end.setUint32(16, offset, true);
  return new Blob([...parts, ...central, new Uint8Array(end.buffer)], { type: 'application/zip' });
}

/** sha256 as lower case hex, for receipt.json. */
export async function sha256(bytes) {
  const d = await crypto.subtle.digest('SHA-256', bytes instanceof Uint8Array ? bytes : enc.encode(String(bytes)));
  return [...new Uint8Array(d)].map(b => b.toString(16).padStart(2, '0')).join('');
}
