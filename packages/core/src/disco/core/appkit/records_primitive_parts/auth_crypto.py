"""Password hashing / constant-time-compare TS emitter for the records-auth
Worker.

Extracted verbatim from ``records_primitive.py`` to reduce module size; the
parent module re-imports every name here unchanged (see its module docstring).
"""

from __future__ import annotations


def _emit_auth_crypto_ts() -> str:
    return (
        "const TEXT_ENCODER = new TextEncoder();\n"
        "const PBKDF2_ITERATIONS = 100000;\n"
        "const PASSWORD_BITS = 256;\n\n"
        'const DUMMY_LOGIN_SALT_HEX = "000102030405060708090a0b0c0d0e0f";\n\n'
        'const DUMMY_LOGIN_HASH_HEX = "'
        '124e4e5ea9e8daf1710f215192eda6bf79ff9ce9b977e7e0d754392319f6fa68";\n\n'
        "function bytesToHex(bytes: Uint8Array): string {\n"
        '  let out = "";\n'
        '  for (const byte of bytes) out += byte.toString(16).padStart(2, "0");\n'
        "  return out;\n"
        "}\n\n"
        "function hexToBytes(hex: string): Uint8Array | null {\n"
        "  if (hex.length % 2 !== 0 || !/^[0-9a-fA-F]*$/.test(hex)) return null;\n"
        "  const bytes = new Uint8Array(hex.length / 2);\n"
        "  for (let i = 0; i < bytes.length; i += 1) {\n"
        "    bytes[i] = Number.parseInt(hex.slice(i * 2, i * 2 + 2), 16);\n"
        "  }\n"
        "  return bytes;\n"
        "}\n\n"
        "function randomHex(byteLength: number): string {\n"
        "  const bytes = new Uint8Array(byteLength);\n"
        "  crypto.getRandomValues(bytes);\n"
        "  return bytesToHex(bytes);\n"
        "}\n\n"
        "async function sha256Hex(value: string): Promise<string> {\n"
        '  const digest = await crypto.subtle.digest("SHA-256", TEXT_ENCODER.encode(value));\n'
        "  return bytesToHex(new Uint8Array(digest));\n"
        "}\n\n"
        "async function derivePasswordHash(password: string, salt: Uint8Array): Promise<string> {\n"
        "  const key = await crypto.subtle.importKey(\n"
        '    "raw",\n'
        "    TEXT_ENCODER.encode(password),\n"
        '    "PBKDF2",\n'
        "    false,\n"
        '    ["deriveBits"]\n'
        "  );\n"
        "  const bits = await crypto.subtle.deriveBits(\n"
        '    { name: "PBKDF2", hash: "SHA-256", salt, iterations: PBKDF2_ITERATIONS },\n'
        "    key,\n"
        "    PASSWORD_BITS\n"
        "  );\n"
        "  return bytesToHex(new Uint8Array(bits));\n"
        "}\n\n"
        "async function hashPassword(password: string): Promise<{ salt: string; hash: string }> {\n"
        "  const salt = new Uint8Array(16);\n"
        "  crypto.getRandomValues(salt);\n"
        "  return { salt: bytesToHex(salt), hash: await derivePasswordHash(password, salt) };\n"
        "}\n\n"
        "async function verifyPassword(\n"
        "  password: string,\n"
        "  saltHex: string,\n"
        "  expectedHashHex: string\n"
        "): Promise<boolean> {\n"
        "  const salt = hexToBytes(saltHex);\n"
        "  if (salt === null) return false;\n"
        "  const actual = await derivePasswordHash(password, salt);\n"
        "  return fixedWorkHexEqual(actual, expectedHashHex);\n"
        "}\n\n"
        "async function spendInvalidLoginWork(password: string): Promise<void> {\n"
        "  const salt = hexToBytes(DUMMY_LOGIN_SALT_HEX);\n"
        "  if (salt === null) return;\n"
        "  const actual = await derivePasswordHash(password, salt);\n"
        "  fixedWorkHexEqual(actual, DUMMY_LOGIN_HASH_HEX);\n"
        "}\n\n"
        "// Workers do not expose timingSafeEqual. Length is checked first, then equal-length\n"
        "// hex strings are compared with XOR accumulation to avoid `===` on derived secrets.\n"
        "function fixedWorkHexEqual(actual: string, expected: string): boolean {\n"
        "  if (actual.length !== expected.length) return false;\n"
        "  let diff = 0;\n"
        "  for (let i = 0; i < actual.length; i += 1) {\n"
        "    diff |= actual.charCodeAt(i) ^ expected.charCodeAt(i);\n"
        "  }\n"
        "  return diff === 0;\n"
        "}\n"
    )
