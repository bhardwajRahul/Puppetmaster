// Warm `codegraph explore` for one workspace.
//
// `codegraph explore` pays Node start, module load and a graph open on every
// call. This helper opens the graph once and answers explore requests over
// stdin/stdout as JSON lines, through the same ToolHandler call the CLI makes
// (no MCP session, so no cross-call dedup): output is byte-identical.
//
// argv: <codegraph lib dist dir> <workspace cwd>
// in:   {"id": n, "query": "...", "maxFiles": 15}
// out:  {"ready": true, "root": "..."} once, then {"id": n, "ok": bool, "text"|"error"}
// Exits when stdin closes (the Python owner went away).
import { createRequire } from 'node:module';
import path from 'node:path';
import readline from 'node:readline';

const require = createRequire(import.meta.url);
const [libDir, cwd] = process.argv.slice(2);
const directory = require(path.join(libDir, 'directory.js'));
const lib = require(path.join(libDir, 'index.js'));
const { ToolHandler } = require(path.join(libDir, 'mcp', 'tools.js'));
const CodeGraph = lib.default || lib.CodeGraph;

// Same resolution as the CLI's resolveProjectPath(): the directory itself, else
// the nearest parent holding an initialized index.
function resolveProjectPath(start) {
  let current = path.resolve(start);
  if (directory.isInitialized(current)) return current;
  const root = path.parse(current).root;
  while (current !== root) {
    const parent = path.dirname(current);
    if (parent === current) break;
    current = parent;
    if (directory.isInitialized(current)) return current;
  }
  return null;
}

const send = (obj) => process.stdout.write(JSON.stringify(obj) + '\n');

const projectPath = resolveProjectPath(cwd);
if (!projectPath) {
  send({ ready: false, error: 'not initialized' });
  process.exit(1);
}
const cg = await CodeGraph.open(projectPath);
const handler = new ToolHandler(cg);
send({ ready: true, root: projectPath });

const rl = readline.createInterface({ input: process.stdin });
for await (const line of rl) {
  let req;
  try {
    req = JSON.parse(line);
  } catch {
    continue;
  }
  try {
    const args = { query: String(req.query || '') };
    if (req.maxFiles) args.maxFiles = Number(req.maxFiles);
    const result = await handler.execute('codegraph_explore', args);
    const text = (result.content && result.content[0] && result.content[0].text) || '';
    send(result.isError ? { id: req.id, ok: false, error: text } : { id: req.id, ok: true, text });
  } catch (err) {
    send({ id: req.id, ok: false, error: String((err && err.message) || err) });
  }
}
cg.destroy();
