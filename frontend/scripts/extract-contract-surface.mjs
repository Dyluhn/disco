#!/usr/bin/env node
/**
 * Extract the frontend's declared wire-contract surface from
 * `src/types/agent.ts`, using the PINNED TypeScript compiler.
 *
 * Why a compiler and not a regex: `WSServerFrame`'s members are type literals
 * whose properties are separated by `;`, so the obvious
 * /export type WSServerFrame =(.*?);/ captures exactly the first member and
 * silently reports a one-variant union. A parity gate that under-reports the
 * frontend's surface passes while the drift it exists to catch is still there.
 * `src/lib/eventDisposition.ts`'s flat string array is simple enough for the
 * regex in test_event_kind_frontend_contract.py; a discriminated union is not.
 *
 * Emits JSON on stdout for `packages/core/tests/test_frontend_contract_parity.py`
 * to compare against the Python authority. Fails closed (exit 2) if any expected
 * declaration is missing, unresolvable, or shaped unexpectedly — never emits a
 * partial surface, because a partial surface reads as agreement.
 *
 * Usage: node scripts/extract-contract-surface.mjs [--repo <root>]
 */

import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FRONTEND = path.resolve(HERE, "..");

function die(message) {
  process.stderr.write(`extract-contract-surface: ${message}\n`);
  process.exit(2);
}

const compilerPath = path.join(FRONTEND, "node_modules", "typescript");
const pkgPath = path.join(compilerPath, "package.json");
if (!fs.existsSync(pkgPath)) {
  die(
    `pinned TypeScript compiler absent at ${compilerPath} — run \`npm ci\` in ` +
      `frontend/ (node_modules is gitignored and per-worktree)`,
  );
}
const REQUIRED_TS_VERSION = "5.9.3";
const compilerVersion = JSON.parse(fs.readFileSync(pkgPath, "utf8")).version;
if (compilerVersion !== REQUIRED_TS_VERSION) {
  die(
    `TypeScript compiler version mismatch: expected ${REQUIRED_TS_VERSION}, ` +
      `got ${compilerVersion}. The contract gate must use the pinned compiler.`,
  );
}
const ts = (await import(path.join(compilerPath, "lib", "typescript.js"))).default;

const target = path.join(FRONTEND, "src", "types", "agent.ts");
if (!fs.existsSync(target)) die(`missing ${target}`);
const text = fs.readFileSync(target, "utf8");
const source = ts.createSourceFile(
  "agent.ts",
  text,
  ts.ScriptTarget.Latest,
  true,
  ts.ScriptKind.TS,
);
if (source.parseDiagnostics && source.parseDiagnostics.length > 0) {
  die(
    `${source.parseDiagnostics.length} parse diagnostic(s) in agent.ts — an ` +
      `unparseable mirror yields an empty surface, which reads as agreement`,
  );
}

/** name -> TypeAliasDeclaration */
const aliases = new Map();
/** name -> InterfaceDeclaration */
const interfaces = new Map();
source.forEachChild((node) => {
  if (ts.isTypeAliasDeclaration(node)) aliases.set(node.name.text, node);
  else if (ts.isInterfaceDeclaration(node)) interfaces.set(node.name.text, node);
});

/** The string literal assigned to a `kind:` / `type:` property, or null. */
function discriminantOf(members, property) {
  for (const member of members) {
    if (
      ts.isPropertySignature(member) &&
      member.name &&
      member.name.getText(source) === property &&
      member.type &&
      ts.isLiteralTypeNode(member.type) &&
      ts.isStringLiteral(member.type.literal)
    ) {
      return member.type.literal.text;
    }
  }
  return null;
}

/**
 * Flatten a union type into its discriminant literals, following references to
 * locally-declared aliases and interfaces. Following references is what lets
 * `WSServerFrame = WSWireServerFrame | WSClientSynthesizedFrame` be transparent
 * to this extractor: the gate can assert on the parts AND on the whole.
 */
function discriminants(node, property, seen = new Set()) {
  const out = [];
  const visit = (type) => {
    if (ts.isParenthesizedTypeNode(type)) return visit(type.type);
    if (ts.isUnionTypeNode(type)) return type.types.forEach(visit);
    if (ts.isTypeLiteralNode(type)) {
      const literal = discriminantOf(type.members, property);
      if (literal === null) {
        die(
          `a member of a ${property}-discriminated union has no literal ` +
            `\`${property}\` property — the surface cannot be extracted safely`,
        );
      }
      out.push(literal);
      return;
    }
    if (ts.isTypeReferenceNode(type)) {
      const name = type.typeName.getText(source);
      if (seen.has(name)) die(`circular type reference through ${name}`);
      seen.add(name);
      if (aliases.has(name)) return visit(aliases.get(name).type);
      if (interfaces.has(name)) {
        const literal = discriminantOf(interfaces.get(name).members, property);
        if (literal === null) {
          die(`interface ${name} has no literal \`${property}\` property`);
        }
        out.push(literal);
        return;
      }
      die(`unresolvable type reference ${name} in a ${property}-union`);
    }
    die(`unsupported node in a ${property}-union: ${ts.SyntaxKind[type.kind]}`);
  };
  visit(node);
  return out;
}

function requireAlias(name) {
  if (!aliases.has(name)) die(`missing required type alias \`${name}\` in agent.ts`);
  return aliases.get(name).type;
}

/** Map each AgentEvent union member interface to its `kind` literal. */
const agentEventUnion = requireAlias("AgentEvent");
const agentEventInterfaces = {};
{
  const visit = (type) => {
    if (ts.isUnionTypeNode(type)) return type.types.forEach(visit);
    if (!ts.isTypeReferenceNode(type)) {
      die(`AgentEvent member is not a named interface: ${ts.SyntaxKind[type.kind]}`);
    }
    const name = type.typeName.getText(source);
    if (!interfaces.has(name)) die(`AgentEvent references unknown interface ${name}`);
    const literal = discriminantOf(interfaces.get(name).members, "kind");
    if (literal === null) die(`interface ${name} has no literal \`kind\` property`);
    if (agentEventInterfaces[literal]) {
      die(
        `two AgentEvent members declare kind "${literal}" ` +
          `(${agentEventInterfaces[literal]} and ${name}) — the union is ambiguous`,
      );
    }
    agentEventInterfaces[literal] = name;
  };
  visit(agentEventUnion);
}

const surface = {
  schema: "disclaude-frontend-contract-surface-v1",
  compiler_version: compilerVersion,
  agent_event_kinds: Object.keys(agentEventInterfaces).sort(),
  agent_event_interfaces: agentEventInterfaces,
  ws_wire_server_frame_types: [
    ...new Set(discriminants(requireAlias("WSWireServerFrame"), "type")),
  ].sort(),
  ws_client_synthesized_frame_types: [
    ...new Set(discriminants(requireAlias("WSClientSynthesizedFrame"), "type")),
  ].sort(),
  ws_server_frame_types: [
    ...new Set(discriminants(requireAlias("WSServerFrame"), "type")),
  ].sort(),
  ws_client_frame_types: [
    ...new Set(discriminants(requireAlias("WSClientFrame"), "type")),
  ].sort(),
};

process.stdout.write(JSON.stringify(surface, null, 2) + "\n");
