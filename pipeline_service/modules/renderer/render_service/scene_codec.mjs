const TYPED_ARRAYS = {
  Int8Array,
  Uint8Array,
  Uint8ClampedArray,
  Int16Array,
  Uint16Array,
  Int32Array,
  Uint32Array,
  Float32Array,
  Float64Array,
};

function typedArrayFor(type) {
  const TA = TYPED_ARRAYS[type];
  if (!TA) throw new Error(`scene_codec: unsupported typed array type "${type}"`);
  return TA;
}

function bytesToBase64(bytes) {
  if (typeof Buffer !== 'undefined') {
    return Buffer.from(bytes.buffer, bytes.byteOffset, bytes.byteLength).toString('base64');
  }
  let binary = '';
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

function base64ToBytes(b64) {
  if (typeof Buffer !== 'undefined') {
    const buf = Buffer.from(b64, 'base64');
    const out = new Uint8Array(buf.length);
    out.set(buf);
    return out;
  }
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

function packArrayField(holder, arrayKey) {
  if (!holder || !Array.isArray(holder[arrayKey]) || typeof holder.type !== 'string') return;
  const TA = typedArrayFor(holder.type);
  const arr = TA.from(holder[arrayKey]);
  const bytes = new Uint8Array(arr.buffer, arr.byteOffset, arr.byteLength);
  holder.__b64 = bytesToBase64(bytes);
  delete holder[arrayKey];
}

function unpackArrayField(holder, arrayKey) {
  if (!holder || typeof holder.__b64 !== 'string' || typeof holder.type !== 'string') return;
  const TA = typedArrayFor(holder.type);
  const bytes = base64ToBytes(holder.__b64);
  const arr = new TA(bytes.buffer, 0, bytes.byteLength / TA.BYTES_PER_ELEMENT);
  holder[arrayKey] = Array.from(arr);
  delete holder.__b64;
}

function eachImageHolder(json, fn) {
  const images = json && json.images;
  if (!Array.isArray(images)) return;
  for (const img of images) {
    if (img && img.url) fn(img.url, 'data');
  }
}

function eachGeometryHolder(json, fn) {
  const geos = json && json.geometries;
  if (!Array.isArray(geos)) return;
  for (const g of geos) {
    const d = g && g.data;
    if (!d) continue;
    if (d.attributes) {
      for (const name of Object.keys(d.attributes)) fn(d.attributes[name], 'array');
    }
    if (d.index) fn(d.index, 'array');
    if (d.morphAttributes) {
      for (const name of Object.keys(d.morphAttributes)) {
        const list = d.morphAttributes[name];
        if (Array.isArray(list)) for (const a of list) fn(a, 'array');
      }
    }
  }
}

export function compactScene(json) {
  eachImageHolder(json, packArrayField);
  eachGeometryHolder(json, packArrayField);
  return json;
}

export function expandScene(json) {
  eachImageHolder(json, unpackArrayField);
  eachGeometryHolder(json, unpackArrayField);
  return json;
}
