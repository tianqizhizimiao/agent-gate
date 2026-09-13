// ============================================================================
// AgentGate 前端密码学：PBKDF2-HMAC-SHA256 与 SHA-256
//
//   优先用 WebCrypto（仅"安全上下文"可用：HTTPS 或 localhost/127.0.0.1）；
//   否则回退到内置的纯 JS 实现 —— 这样通过局域网 IP 走 HTTP 访问时，
//   密码依然不会被明文发送。
//
// 参数必须与服务端 app/auth.py 的 PBKDF2_ITERATIONS / PBKDF2_ALGO 保持一致。
// ============================================================================

const AG_PBKDF2_ITERATIONS = 200000;

const AG_HAS_WEBCRYPTO = (() => {
  try {
    return !!(window.crypto && window.crypto.subtle && window.crypto.subtle.importKey);
  } catch (e) {
    return false;
  }
})();

function agHex(bytes) {
  let s = "";
  for (let i = 0; i < bytes.length; i++) s += bytes[i].toString(16).padStart(2, "0");
  return s;
}

function agFromHex(hex) {
  const out = new Uint8Array(hex.length >> 1);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.substr(i * 2, 2), 16);
  return out;
}

function agCat(a, b) {
  const o = new Uint8Array(a.length + b.length);
  o.set(a, 0);
  o.set(b, a.length);
  return o;
}

// ---- 纯 JS SHA-256 ---------------------------------------------------------
const AG_K = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);

function agRotr(x, n) { return ((x >>> n) | (x << (32 - n))) >>> 0; }

function agSha256(bytes) {
  const H = new Uint32Array([0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
                             0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19]);
  const len = bytes.length;
  const total = (((len + 8) >> 6) + 1) << 6;      // 补 0x80 + 64bit 长度
  const buf = new Uint8Array(total);
  buf.set(bytes, 0);
  buf[len] = 0x80;
  const dv = new DataView(buf.buffer);
  const bitLen = len * 8;
  dv.setUint32(total - 8, Math.floor(bitLen / 0x100000000));
  dv.setUint32(total - 4, bitLen >>> 0);

  const w = new Uint32Array(64);
  for (let off = 0; off < total; off += 64) {
    for (let i = 0; i < 16; i++) w[i] = dv.getUint32(off + i * 4);
    for (let i = 16; i < 64; i++) {
      const a15 = w[i - 15], a2 = w[i - 2];
      const s0 = (agRotr(a15, 7) ^ agRotr(a15, 18) ^ (a15 >>> 3)) >>> 0;
      const s1 = (agRotr(a2, 17) ^ agRotr(a2, 19) ^ (a2 >>> 10)) >>> 0;
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0;
    }
    let a = H[0], b = H[1], c = H[2], d = H[3], e = H[4], f = H[5], g = H[6], h = H[7];
    for (let i = 0; i < 64; i++) {
      const S1 = (agRotr(e, 6) ^ agRotr(e, 11) ^ agRotr(e, 25)) >>> 0;
      const ch = ((e & f) ^ (~e & g)) >>> 0;
      const t1 = (h + S1 + ch + AG_K[i] + w[i]) >>> 0;
      const S0 = (agRotr(a, 2) ^ agRotr(a, 13) ^ agRotr(a, 22)) >>> 0;
      const maj = ((a & b) ^ (a & c) ^ (b & c)) >>> 0;
      const t2 = (S0 + maj) >>> 0;
      h = g; g = f; f = e; e = (d + t1) >>> 0;
      d = c; c = b; b = a; a = (t1 + t2) >>> 0;
    }
    H[0] = (H[0] + a) >>> 0; H[1] = (H[1] + b) >>> 0;
    H[2] = (H[2] + c) >>> 0; H[3] = (H[3] + d) >>> 0;
    H[4] = (H[4] + e) >>> 0; H[5] = (H[5] + f) >>> 0;
    H[6] = (H[6] + g) >>> 0; H[7] = (H[7] + h) >>> 0;
  }
  const out = new Uint8Array(32);
  const odv = new DataView(out.buffer);
  for (let i = 0; i < 8; i++) odv.setUint32(i * 4, H[i]);
  return out;
}

function agHmacPad(key) {
  let k = key;
  if (k.length > 64) k = agSha256(k);
  const ipad = new Uint8Array(64).fill(0x36);
  const opad = new Uint8Array(64).fill(0x5c);
  for (let i = 0; i < k.length; i++) { ipad[i] ^= k[i]; opad[i] ^= k[i]; }
  return { ipad, opad };
}

function agHmacSha256(key, msg) {
  const { ipad, opad } = agHmacPad(key);
  return agSha256(agCat(opad, agSha256(agCat(ipad, msg))));
}

function agPbkdf2Js(passwordBytes, saltBytes, iterations) {
  const { ipad, opad } = agHmacPad(passwordBytes);         // 密钥固定，pads 只算一次
  const hmac = (data) => agSha256(agCat(opad, agSha256(agCat(ipad, data))));
  let u = hmac(agCat(saltBytes, new Uint8Array([0, 0, 0, 1])));   // dkLen=32 → 1 块
  const out = u.slice();
  for (let i = 1; i < iterations; i++) {
    u = hmac(u);
    for (let j = 0; j < out.length; j++) out[j] ^= u[j];
  }
  return out;
}

// ---- 对外 API --------------------------------------------------------------
// verifier = PBKDF2-HMAC-SHA256(密码, salt, iterations)，返回 16 进制
async function agPbkdf2(password, saltHex, iterations) {
  const iters = iterations || AG_PBKDF2_ITERATIONS;
  const pw = new TextEncoder().encode(password);
  const salt = agFromHex(saltHex);
  if (AG_HAS_WEBCRYPTO) {
    const key = await crypto.subtle.importKey("raw", pw, "PBKDF2", false, ["deriveBits"]);
    const bits = await crypto.subtle.deriveBits(
      { name: "PBKDF2", salt: salt, iterations: iters, hash: "SHA-256" }, key, 256);
    return agHex(new Uint8Array(bits));
  }
  return agHex(agPbkdf2Js(pw, salt, iters));
}

// proof = SHA256(nonce || verifier)，返回 16 进制
async function agProof(nonceHex, verifierHex) {
  const data = agCat(agFromHex(nonceHex), agFromHex(verifierHex));
  if (AG_HAS_WEBCRYPTO) {
    const bits = await crypto.subtle.digest("SHA-256", data);
    return agHex(new Uint8Array(bits));
  }
  return agHex(agSha256(data));
}
