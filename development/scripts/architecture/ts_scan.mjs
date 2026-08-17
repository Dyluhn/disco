#!/usr/bin/env node
// Read-only TypeScript/TSX structure scanner using the pinned TypeScript compiler.
// Invoked by development/scripts/architecture/typescript_scan.py.
//
// Enforces frontend context cycles and cross-feature edges using the checked
// context registry, and detects raw fetch/WebSocket/EventSource transport in
// components/hooks (typed calls through current/frontend/src/api pass).

import fs from "node:fs";
import path from "node:path";
import childProcess from "node:child_process";
import { createRequire } from "node:module";

function stripBucket(value) {
  // The 2026-08-17 restructure moved every tracked path under current/ or
  // development/; compare on the logical path so a move is not a rename.
  for (const bucket of ["current/", "development/"]) {
    if (value.startsWith(bucket)) return value.slice(bucket.length);
  }
  return value;
}

function resolveInRepo(repo, rel) {
  // Emitted paths are logical (bucket stripped); the file may live under
  // current/ in the real tree or at the bare label in a fixture tree.
  for (const candidate of [path.join(repo, "current", rel), path.join(repo, rel)]) {
    if (fs.existsSync(candidate)) return candidate;
  }
  return path.join(repo, rel);
}

function main() {
  const repoIndex = process.argv.indexOf("--repo");
  const compilerIndex = process.argv.indexOf("--compiler");
  if (repoIndex < 0 || compilerIndex < 0) {
    throw new Error("usage: ts_scan.mjs --repo PATH --compiler PATH");
  }
  const repo = process.argv[repoIndex + 1];
  const compilerPath = process.argv[compilerIndex + 1];

  const require = createRequire(import.meta.url);
  const ts = require(path.join(compilerPath, "lib", "typescript.js"));

  // Load frontend contexts from the requested root. Missing or malformed
  // authority is a hard failure; there is no live-tree fallback.
  // Locate the registry rather than assuming one layout: the real tree keeps it
  // under development/, a synthetic fixture tree keeps it at the bare label.
  let contextsPath = path.join(repo, "development", "architecture", "contexts.json");
  if (!fs.existsSync(contextsPath)) {
    contextsPath = path.join(repo, "architecture", "contexts.json");
  }
  if (!fs.existsSync(contextsPath)) {
    throw new Error(`contexts registry missing: ${contextsPath}`);
  }
  const contexts = JSON.parse(fs.readFileSync(contextsPath, "utf8"));
  if (contexts.schema !== "disclaude-architecture-contexts-v1") {
    throw new Error("contexts registry schema mismatch");
  }
  const frontendAuthority = contexts.bounded_contexts?.frontend;
  if (
    !frontendAuthority ||
    stripBucket(frontendAuthority.root ?? "") !== "frontend/src" ||
    frontendAuthority.cross_feature_imports_rejected !== true ||
    typeof frontendAuthority.contexts !== "object" ||
    !frontendAuthority.dm007_legacy
  ) {
    throw new Error("frontend context/DM-007 authority is incomplete");
  }
  const frontendContexts = frontendAuthority.contexts;
  const crossFeatureRejected = true;
  const dm007 = frontendAuthority.dm007_legacy;
  const expectedEdgeFields = [
    "source",
    "source_context",
    "import",
    "target",
    "target_context",
  ];
  if (
    dm007.observation_id !== "DM-007" ||
    dm007.source_identity !== contexts.source_identity ||
    dm007.owner_package !== "PKG-12-FE-BUILD" ||
    JSON.stringify(dm007.edge_fields) !== JSON.stringify(expectedEdgeFields) ||
    !Array.isArray(dm007.edges) ||
    !dm007.edges.length ||
    !Array.isArray(dm007.cycles)
  ) {
    throw new Error("DM-007 legacy authority schema mismatch");
  }
  for (const [name, definition] of Object.entries(frontendContexts)) {
    if (
      !name ||
      !definition ||
      !Array.isArray(definition.modules) ||
      !definition.modules.length ||
      definition.modules.some(
        (item) => typeof item !== "string" || !item || item.includes("*"),
      )
    ) {
      throw new Error(`invalid frontend context definition: ${name}`);
    }
  }
  for (const edge of dm007.edges) {
    if (
      !Array.isArray(edge) ||
      edge.length !== expectedEdgeFields.length ||
      edge.some((item) => typeof item !== "string" || !item || item.includes("*"))
    ) {
      throw new Error("invalid DM-007 legacy edge");
    }
  }
  if (new Set(dm007.edges.map((edge) => JSON.stringify(edge))).size !== dm007.edges.length) {
    throw new Error("duplicate DM-007 legacy edge");
  }

  // Build a module-to-context map from the frontend context definitions.
  // Each context lists module prefixes (e.g. "api/agent", "hooks/useBuild").
  // A file is classified by matching its path relative to current/frontend/src/ against
  // these prefixes. No filename-only classification.
  const moduleToContext = new Map();
  for (const [ctxName, ctxDef] of Object.entries(frontendContexts)) {
    const modules = ctxDef.modules || [];
    for (const mod of modules) {
      // Normalize: remove " (shared)" suffixes, use as prefix match
      const normalized = mod.replace(/\s*\(.*\)$/, "");
      moduleToContext.set(normalized, ctxName);
    }
  }

  function classifyContext(rel) {
    // rel is like "current/frontend/src/components/BuildSurface.tsx" in the real
    // tree and "frontend/src/..." in a fixture tree; normalise the bucket away
    // first, then strip the frontend src prefix for matching.
    const logical = stripBucket(rel);
    const relFromSrc = logical.startsWith("frontend/src/")
      ? logical.slice("frontend/src/".length)
      : logical;
    // Remove extension
    const withoutExt = relFromSrc.replace(/\.[jt]sx?$/, "");
    // Match against module prefixes — longest match wins
    let bestMatch = null;
    let bestLen = 0;
    for (const [modPrefix, ctxName] of moduleToContext) {
      // Match if the file path starts with the module prefix
      // (e.g. "hooks/useBuild" matches "hooks/useBuild.ts" and "hooks/useBuildStream.ts")
      if (
        withoutExt === modPrefix ||
        withoutExt.startsWith(modPrefix + "/") ||
        withoutExt.startsWith(modPrefix + ".")
      ) {
        if (modPrefix.length > bestLen) {
          bestMatch = ctxName;
          bestLen = modPrefix.length;
        }
      }
    }
    return bestMatch;
  }

  function trackedFiles() {
    return childProcess
      .execFileSync("git", ["-C", repo, "ls-files", "-z"])
      .toString("utf8")
      .split("\0")
      .filter(
        (rel) =>
          rel &&
          stripBucket(rel).startsWith("frontend/src/") &&
          (rel.endsWith(".ts") || rel.endsWith(".tsx")),
      )
      .map(stripBucket);
  }

  function lineNumber(source, position) {
    return source.getLineAndCharacterOfPosition(position).line + 1;
  }

  function inclusiveLines(source, node) {
    const start = lineNumber(source, node.getStart(source));
    const end = lineNumber(source, node.getEnd());
    return { start, end, physical: end - start + 1 };
  }

  function logicalLineSet(text, scriptKind) {
    const scanner = ts.createScanner(
      ts.ScriptTarget.Latest,
      false,
      scriptKind,
      text,
    );
    const lineStarts = [0];
    for (let i = 0; i < text.length; i++) {
      if (text[i] === "\n") lineStarts.push(i + 1);
    }
    const content = new Set();
    let token;
    do {
      token = scanner.scan();
      if (
        token === ts.SyntaxKind.WhitespaceTrivia ||
        token === ts.SyntaxKind.NewLineTrivia ||
        token === ts.SyntaxKind.SingleLineCommentTrivia ||
        token === ts.SyntaxKind.MultiLineCommentTrivia ||
        token === ts.SyntaxKind.EndOfFileToken
      ) {
        continue;
      }
      const startLine = binaryLine(lineStarts, scanner.getTokenPos()) + 1;
      const endLine = binaryLine(lineStarts, scanner.getTextPos()) + 1;
      for (let line = startLine; line <= endLine; line++) content.add(line);
    } while (token !== ts.SyntaxKind.EndOfFileToken);
    return content;
  }

  function binaryLine(starts, position) {
    let lo = 0;
    let hi = starts.length - 1;
    while (lo <= hi) {
      const mid = Math.floor((lo + hi) / 2);
      if (starts[mid] <= position) lo = mid + 1;
      else hi = mid - 1;
    }
    return Math.max(0, hi);
  }

  function logicalWithin(logical, start, end) {
    let count = 0;
    for (const line of logical) if (line >= start && line <= end) count++;
    return count;
  }

  function hasJsx(node) {
    let result = false;
    function visit(child) {
      if (
        ts.isJsxElement(child) ||
        ts.isJsxSelfClosingElement(child) ||
        ts.isJsxFragment(child)
      ) {
        result = true;
        return;
      }
      if (!result) ts.forEachChild(child, visit);
    }
    visit(node);
    return result;
  }

  function astMcCabe(node) {
    let value = 1;
    function visit(child) {
      if (
        ts.isFunctionDeclaration(child) ||
        ts.isFunctionExpression(child) ||
        ts.isArrowFunction(child) ||
        ts.isMethodDeclaration(child) ||
        ts.isClassDeclaration(child) ||
        ts.isClassExpression(child)
      ) {
        return;
      }
      if (
        ts.isIfStatement(child) ||
        ts.isForStatement(child) ||
        ts.isForInStatement(child) ||
        ts.isForOfStatement(child) ||
        ts.isWhileStatement(child) ||
        ts.isDoStatement(child) ||
        ts.isConditionalExpression(child) ||
        ts.isCatchClause(child)
      ) {
        value++;
      } else if (ts.isCaseClause(child)) {
        value++;
      } else if (
        ts.isBinaryExpression(child) &&
        (child.operatorToken.kind === ts.SyntaxKind.AmpersandAmpersandToken ||
          child.operatorToken.kind === ts.SyntaxKind.BarBarToken ||
          child.operatorToken.kind === ts.SyntaxKind.QuestionQuestionToken)
      ) {
        value++;
      }
      ts.forEachChild(child, visit);
    }
    ts.forEachChild(node, visit);
    return value;
  }

  function isTest(rel) {
    return (
      /\.(test|spec)\.[jt]sx?$/.test(rel) ||
      rel.includes("/test/") ||
      rel.includes("/__tests__/")
    );
  }

  // Resolve a relative import to a tracked file path.
  // Returns the resolved path relative to repo root, or null if not found.
  function resolveImport(sourceRel, imp) {
    // imp is like "@/api/client" or "./sibling" or "../parent"
    // sourceRel is like "current/frontend/src/components/BuildSurface.tsx"
    const sourceDir = path.dirname(sourceRel); // current/frontend/src/components
    let targetRel;
    if (imp.startsWith("@/")) {
      // @/ maps to the frontend src root, expressed logically.
      targetRel = "frontend/src/" + imp.slice(2);
    } else if (imp.startsWith("./")) {
      targetRel = path.normalize(path.join(sourceDir, imp));
    } else if (imp.startsWith("../")) {
      targetRel = path.normalize(path.join(sourceDir, imp));
    } else {
      return null;
    }
    // Try with extensions
    for (const ext of [".ts", ".tsx", ".js", ".jsx", "/index.ts", "/index.tsx"]) {
      const candidate = targetRel + ext;
      const full = resolveInRepo(repo, candidate);
      if (fs.existsSync(full)) {
        return stripBucket(candidate.replace(/\\/g, "/"));
      }
    }
    return null;
  }

  // Detect raw transport calls (fetch, WebSocket, EventSource) in the AST.
  // Uses the compiler AST — no marker/substring search. Typed calls through
  // current/frontend/src/api (including agentFetch) pass because they are wrappers
  // that call fetch internally but are not raw fetch calls in the component/hook.
  function rawTransportCalls(source) {
    const calls = [];
    function visit(node) {
      if (ts.isCallExpression(node)) {
        const expr = node.expression;
        // Direct fetch(...) call
        if (ts.isIdentifier(expr) && expr.text === "fetch") {
          calls.push({
            transport: "fetch",
            line: lineNumber(source, node.getStart(source)),
          });
        }
        // new WebSocket(...) — detected via NewExpression
      }
      if (ts.isNewExpression(node)) {
        const expr = node.expression;
        if (ts.isIdentifier(expr)) {
          if (expr.text === "WebSocket") {
            calls.push({
              transport: "WebSocket",
              line: lineNumber(source, node.getStart(source)),
            });
          }
          if (expr.text === "EventSource") {
            calls.push({
              transport: "EventSource",
              line: lineNumber(source, node.getStart(source)),
            });
          }
        }
      }
      ts.forEachChild(node, visit);
    }
    visit(source);
    return calls;
  }

  function imports(source) {
    const result = new Set();
    const nonliteral = [];
    function visit(node) {
      if (
        ts.isImportDeclaration(node) &&
        node.moduleSpecifier &&
        ts.isStringLiteral(node.moduleSpecifier)
      ) {
        result.add(node.moduleSpecifier.text);
      }
      if (
        ts.isExportDeclaration(node) &&
        node.moduleSpecifier &&
        ts.isStringLiteral(node.moduleSpecifier)
      ) {
        result.add(node.moduleSpecifier.text);
      }
      if (
        ts.isCallExpression(node) &&
        node.expression.kind === ts.SyntaxKind.ImportKeyword
      ) {
        const argument = node.arguments[0];
        if (
          argument &&
          (ts.isStringLiteral(argument) ||
            ts.isNoSubstitutionTemplateLiteral(argument))
        ) {
          result.add(argument.text);
        } else {
          nonliteral.push({
            line: lineNumber(source, node.getStart(source)),
          });
        }
      }
      ts.forEachChild(node, visit);
    }
    visit(source);
    return { values: [...result].sort(), nonliteral };
  }

  function normalizedText(node, source) {
    return node.getText(source).replace(/\s+/g, " ").trim();
  }

  function withoutBody(node, source) {
    if (!node.body) return normalizedText(node, source);
    const start = node.getStart(source);
    return source.text
      .slice(start, node.body.getStart(source))
      .replace(/\s+/g, " ")
      .trim();
  }

  function classSurface(node, source) {
    const text = node.getText(source);
    const header = text.slice(0, text.indexOf("{")).replace(/\s+/g, " ").trim();
    const members = node.members
      .filter(
        (member) =>
          !member.modifiers?.some(
            (modifier) =>
              modifier.kind === ts.SyntaxKind.PrivateKeyword ||
              modifier.kind === ts.SyntaxKind.ProtectedKeyword,
          ),
      )
      .map((member) => withoutBody(member, source))
      .sort();
    return `${header} { ${members.join("; ")} }`;
  }

  function declarationName(node, source) {
    if (node.name) return node.name.getText(source);
    return node.modifiers?.some(
      (modifier) => modifier.kind === ts.SyntaxKind.DefaultKeyword,
    )
      ? "default"
      : "<anonymous>";
  }

  function publicDeclarations(source) {
    const rows = [];
    for (const node of source.statements) {
      if (ts.isExportDeclaration(node) || ts.isExportAssignment(node)) {
        rows.push({
          kind: ts.isExportDeclaration(node) ? "reexport" : "export_assignment",
          name: "export",
          signature: normalizedText(node, source),
        });
        continue;
      }
      if (!exported(node)) continue;
      if (ts.isFunctionDeclaration(node)) {
        rows.push({
          kind: "function",
          name: declarationName(node, source),
          signature: withoutBody(node, source),
        });
      } else if (ts.isClassDeclaration(node)) {
        rows.push({
          kind: "class",
          name: declarationName(node, source),
          signature: classSurface(node, source),
        });
      } else if (
        ts.isInterfaceDeclaration(node) ||
        ts.isTypeAliasDeclaration(node) ||
        ts.isEnumDeclaration(node)
      ) {
        rows.push({
          kind: ts.SyntaxKind[node.kind],
          name: declarationName(node, source),
          signature: normalizedText(node, source),
        });
      } else if (ts.isVariableStatement(node)) {
        rows.push({
          kind: "variable",
          name: node.declarationList.declarations
            .map((item) => item.name.getText(source))
            .sort()
            .join(","),
          signature: normalizedText(node, source),
        });
      }
    }
    return rows.sort((left, right) =>
      JSON.stringify(left).localeCompare(JSON.stringify(right)),
    );
  }

  function declarationSymbols(source) {
    const rows = [];
    function add(name, node) {
      const span = inclusiveLines(source, node);
      rows.push({
        name,
        line_start: span.start,
        line_end: span.end,
      });
    }
    for (const node of source.statements) {
      if (
        ts.isFunctionDeclaration(node) ||
        ts.isClassDeclaration(node) ||
        ts.isInterfaceDeclaration(node) ||
        ts.isTypeAliasDeclaration(node) ||
        ts.isEnumDeclaration(node)
      ) {
        if (node.name) add(node.name.getText(source), node);
      } else if (ts.isVariableStatement(node)) {
        for (const declaration of node.declarationList.declarations) {
          if (ts.isIdentifier(declaration.name)) {
            add(declaration.name.text, declaration);
          }
        }
      }
    }
    return rows.sort((left, right) =>
      JSON.stringify(left).localeCompare(JSON.stringify(right)),
    );
  }

  function exported(node) {
    return Boolean(
      node.modifiers?.some(
        (modifier) => modifier.kind === ts.SyntaxKind.ExportKeyword,
      ),
    );
  }

  function scan(rel) {
    const full = resolveInRepo(repo, rel);
    const text = fs.readFileSync(full, "utf8");
    const splitLines = text.split(/\r?\n/);
    const physicalLines =
      text.length === 0 ? 0 : splitLines.length - (text.endsWith("\n") ? 1 : 0);
    const kind = rel.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
    const source = ts.createSourceFile(
      rel,
      text,
      ts.ScriptTarget.Latest,
      true,
      kind,
    );
    const logical = logicalLineSet(text, kind);
    const test = isTest(rel);
    const symbols = [];
    const violations = [];
    const publicTop = [];

    function recordFunction(node, name, symbolKind, isPublic = false) {
      const span = inclusiveLines(source, node);
      const logicalSize = logicalWithin(logical, span.start, span.end);
      const component = /^[A-Z]/.test(name) && hasJsx(node);
      const hook = /^use[A-Z0-9_]/.test(name);
      const row = {
        kind: symbolKind,
        name,
        line_start: span.start,
        line_end: span.end,
        physical_span: span.physical,
        policy_logical_loc: logicalSize,
        ast_mccabe: astMcCabe(node),
        react_component_candidate: component,
        react_hook_candidate: hook,
        exported: isPublic,
      };
      symbols.push(row);
      if (isPublic) publicTop.push(name);
      if (!test && component && logicalSize > 250) {
        violations.push({
          rule: "react_component_logical_gt_250",
          symbol: name,
          line_start: span.start,
          line_end: span.end,
          value: logicalSize,
          limit: 250,
        });
      }
      if (!test && hook && logicalSize > 200) {
        violations.push({
          rule: "react_hook_logical_gt_200",
          symbol: name,
          line_start: span.start,
          line_end: span.end,
          value: logicalSize,
          limit: 200,
        });
      }
      if (!test && row.ast_mccabe > 15) {
        violations.push({
          rule: "typescript_callable_ast_mccabe_gt_15",
          symbol: name,
          line_start: span.start,
          line_end: span.end,
          value: row.ast_mccabe,
          limit: 15,
        });
      }
    }

    function visit(node) {
      if (ts.isFunctionDeclaration(node) && node.name) {
        recordFunction(node, node.name.text, "function", exported(node));
      } else if (ts.isVariableStatement(node)) {
        const isPublic = exported(node);
        node.declarationList.declarations.forEach((declaration) => {
          if (
            ts.isIdentifier(declaration.name) &&
            declaration.initializer &&
            (ts.isArrowFunction(declaration.initializer) ||
              ts.isFunctionExpression(declaration.initializer))
          ) {
            recordFunction(
              declaration.initializer,
              declaration.name.text,
              "variable_function",
              isPublic,
            );
          }
        });
      } else if (ts.isClassDeclaration(node) && node.name) {
        const span = inclusiveLines(source, node);
        const logicalSize = logicalWithin(logical, span.start, span.end);
        const methods = node.members.filter(
          (member) =>
            ts.isMethodDeclaration(member) &&
            member.name &&
            !member.name.getText(source).startsWith("_") &&
            !member.modifiers?.some(
              (modifier) => modifier.kind === ts.SyntaxKind.PrivateKeyword,
            ),
        );
        symbols.push({
          kind: "class",
          name: node.name.text,
          line_start: span.start,
          line_end: span.end,
          physical_span: span.physical,
          policy_logical_loc: logicalSize,
          public_method_count: methods.length,
          exported: exported(node),
        });
        if (exported(node)) publicTop.push(node.name.text);
      }
      ts.forEachChild(node, visit);
    }
    visit(source);

    const moduleLimit = test ? 1200 : 500;
    if (logical.size > moduleLimit) {
      violations.push({
        rule: test
          ? "test_module_logical_gt_1200"
          : "typescript_module_logical_gt_500",
        symbol: "<module>",
        line_start: 1,
        line_end: physicalLines,
        value: logical.size,
        limit: moduleLimit,
      });
    }

    // Classify frontend context and detect cross-feature imports.
    // Cross-feature imports are reported as findings (cross_feature_imports)
    // but NOT as violations — the budget checker's debt ledger tracks known
    // DM-007 legacy cross-feature imports. Only frontend context cycles
    // (which indicate a structural break, not a single edge) are violations.
    const fileContext = classifyContext(rel);
    const importEvidence = imports(source);
    const moduleImports = importEvidence.values;
    const crossFeatureImports = [];
    if (crossFeatureRejected && fileContext && !test) {
      for (const imp of moduleImports) {
        // Only check relative imports (starting with ./ or ../ or @/)
        // External packages (react, lucide-react, etc.) are not cross-feature
        if (!imp.startsWith(".") && !imp.startsWith("@/")) continue;
        // Resolve the import to a file path relative to current/frontend/src/
        const resolvedRel = resolveImport(rel, imp);
        if (!resolvedRel) continue;
        const targetContext = classifyContext(resolvedRel);
        if (targetContext && targetContext !== fileContext) {
          crossFeatureImports.push({
            from_context: fileContext,
            to_context: targetContext,
            import: imp,
            resolved: resolvedRel,
          });
        }
      }
    }

    // Detect raw transport calls in components/hooks (not in api/ layer)
    const isComponentOrHook =
      rel.includes("/components/") || rel.includes("/hooks/");
    const isApiLayer = rel.includes("/api/");
    const transportCalls = [];
    if (isComponentOrHook && !isApiLayer && !test) {
      const rawCalls = rawTransportCalls(source);
      for (const call of rawCalls) {
        transportCalls.push(call);
        violations.push({
          rule: "raw_transport_in_component_or_hook",
          symbol: call.transport,
          line_start: call.line,
          line_end: call.line,
          value: 1,
          limit: 0,
          detail: `raw ${call.transport}() at line ${call.line}`,
        });
      }
    }

    return {
      path: rel,
      root: "current/frontend/src",
      is_test: test,
      physical_loc: physicalLines,
      policy_logical_loc: logical.size,
      public_top_level_symbols: [...new Set(publicTop)].sort(),
      public_declarations: publicDeclarations(source),
      declaration_symbols: declarationSymbols(source),
      imports: moduleImports,
      symbols,
      violations,
      frontend_context: fileContext,
      cross_feature_imports: crossFeatureImports,
      nonliteral_dynamic_imports: importEvidence.nonliteral,
      raw_transport_calls: transportCalls,
      parse_diagnostics: source.parseDiagnostics.map((item) => ({
        code: item.code,
        start: item.start,
        message: ts.flattenDiagnosticMessageText(item.messageText, "\n"),
      })),
    };
  }

  const files = trackedFiles();
  const modules = files.map(scan);

  // Detect frontend context cycles: build a context-level dependency graph
  // from cross-feature imports and check for cycles using DFS.
  const contextGraph = new Map();
  for (const mod of modules) {
    if (!mod.frontend_context) continue;
    for (const xfi of mod.cross_feature_imports || []) {
      const from = xfi.from_context;
      const to = xfi.to_context;
      if (!contextGraph.has(from)) contextGraph.set(from, new Set());
      contextGraph.get(from).add(to);
    }
  }
  const contextCycles = [];
  const cycleRows = new Map();
  function canonicalCycle(path) {
    const nodes = path.slice(0, -1);
    const rotations = nodes.map((_, index) => [
      ...nodes.slice(index),
      ...nodes.slice(0, index),
    ]);
    rotations.sort((left, right) =>
      JSON.stringify(left).localeCompare(JSON.stringify(right)),
    );
    return [...rotations[0], rotations[0][0]];
  }
  function findCycles(start, node, path, seen) {
    const neighbors = [...(contextGraph.get(node) || [])].sort();
    for (const neighbor of neighbors) {
      if (neighbor === start && path.length > 1) {
        const cycle = canonicalCycle([...path, start]);
        cycleRows.set(JSON.stringify(cycle), cycle);
      } else if (!seen.has(neighbor)) {
        findCycles(
          start,
          neighbor,
          [...path, neighbor],
          new Set([...seen, neighbor]),
        );
      }
    }
  }
  for (const node of [...contextGraph.keys()].sort()) {
    findCycles(node, node, [node], new Set([node]));
  }
  contextCycles.push(...[...cycleRows.values()].sort((left, right) =>
    JSON.stringify(left).localeCompare(JSON.stringify(right)),
  ));
  function canonicalRows(rows) {
    return [...rows].map((row) => JSON.stringify(row)).sort();
  }
  const actualLegacyEdges = [];
  for (const mod of modules) {
    for (const edge of mod.cross_feature_imports || []) {
      actualLegacyEdges.push([
        mod.path,
        edge.from_context,
        edge.import,
        edge.resolved,
        edge.to_context,
      ]);
    }
  }
  const contextPolicyProblems = [];
  for (const module of modules) {
    if (module.is_test) continue;
    for (const row of module.nonliteral_dynamic_imports || []) {
      contextPolicyProblems.push({
        rule: "typescript_nonliteral_dynamic_import",
        path: module.path,
        line: row.line,
      });
    }
  }
  const expectedEdges = canonicalRows(dm007.edges);
  const actualEdges = canonicalRows(actualLegacyEdges);
  if (JSON.stringify(expectedEdges) !== JSON.stringify(actualEdges)) {
    const expectedSet = new Set(expectedEdges);
    const actualSet = new Set(actualEdges);
    contextPolicyProblems.push({
      rule: "dm007_exact_legacy_edge_drift",
      deleted: expectedEdges.filter((row) => !actualSet.has(row)),
      added: actualEdges.filter((row) => !expectedSet.has(row)),
    });
  }
  const expectedCycles = canonicalRows(dm007.cycles);
  const actualCycles = canonicalRows(contextCycles);
  if (JSON.stringify(expectedCycles) !== JSON.stringify(actualCycles)) {
    contextPolicyProblems.push({
      rule: "dm007_exact_legacy_cycle_drift",
      expected: expectedCycles,
      actual: actualCycles,
    });
  }

  const payload = {
    schema: "disclaude-architecture-typescript-scan-v1",
    module_count: modules.length,
    violation_count: modules.reduce(
      (total, module) => total + module.violations.length,
      0,
    ),
    parse_diagnostics_count: modules.reduce(
      (total, module) => total + module.parse_diagnostics.length,
      0,
    ),
    frontend_context_cycles: contextCycles,
    frontend_context_policy_problems: contextPolicyProblems,
    dm007_legacy_edge_count: actualLegacyEdges.length,
    frontend_cross_feature_import_count: modules.reduce(
      (total, module) => total + (module.cross_feature_imports || []).length,
      0,
    ),
    raw_transport_call_count: modules.reduce(
      (total, module) => total + (module.raw_transport_calls || []).length,
      0,
    ),
    modules,
  };
  console.log(JSON.stringify(payload, null, 2));
}

main();
