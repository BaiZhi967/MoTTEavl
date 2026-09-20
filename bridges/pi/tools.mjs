/**
 * Bridge-side workspace tools for the Pi agent (M4-T03).
 *
 * Tools execute inside the bridge process but under a strict workspace
 * sandbox: relative paths only, no `..`, no symlink escapes, byte/file
 * quotas. Enforcement owner is the platform (this bridge is platform
 * code); the sandbox is honest about what it enforces.
 */
import { lstat, readdir, readFile, writeFile, mkdir } from "node:fs/promises";
import path from "node:path";

export const WORKSPACE_TOOLS = ["read_file", "write_file", "list_files"];

const DEFAULT_QUOTAS = {
  maxFileBytes: 1_000_000,
  maxTotalBytes: 10_000_000,
  maxFiles: 200,
};

export class WorkspacePolicyError extends Error {
  constructor(code, message) {
    super(message);
    this.code = code;
  }
}

function safeRelative(p) {
  if (typeof p !== "string" || p.length === 0) {
    throw new WorkspacePolicyError("PATH_INVALID", "path must be a non-empty string");
  }
  if (p === "." || p === ".." || p.startsWith("/") || p.startsWith("\\") ||
      /^[A-Za-z]:/.test(p) || p.includes("\0")) {
    throw new WorkspacePolicyError("PATH_INVALID", `path is not workspace-relative: ${p}`);
  }
  const parts = p.split(/[/\\]/).filter((part) => part.length > 0);
  if (parts.includes("..")) {
    throw new WorkspacePolicyError("PATH_ESCAPE", "path must not traverse outside the workspace");
  }
  return parts.join(path.sep);
}

class Workspace {
  constructor(root, quotas = {}) {
    this.root = path.resolve(root);
    this.quotas = { ...DEFAULT_QUOTAS, ...quotas };
    this.totalBytes = 0;
    this.files = new Set();
  }

  /** Resolve and verify every component against symlink substitution. */
  async resolveTarget(relative, { mustExist = false, create = false } = {}) {
    const relativePath = safeRelative(relative);
    let current = this.root;
    const parts = relativePath.split(path.sep);
    for (let index = 0; index < parts.length; index += 1) {
      current = path.join(current, parts[index]);
      let stat;
      try {
        stat = await lstat(current);
      } catch (error) {
        if (error.code === "ENOENT") {
          if (create && index === parts.length - 1) {
            return current;
          }
          if (mustExist) {
            throw new WorkspacePolicyError("PATH_MISSING", `path does not exist: ${relative}`);
          }
          return current;
        }
        throw error;
      }
      if (stat.isSymbolicLink()) {
        throw new WorkspacePolicyError("PATH_SYMLINK", `symlink in workspace path: ${relative}`);
      }
    }
    return current;
  }

  async enforceWriteQuota(relative, bytes) {
    if (bytes > this.quotas.maxFileBytes) {
      throw new WorkspacePolicyError("QUOTA_FILE", `file exceeds per-file quota: ${relative}`);
    }
    if (this.totalBytes + bytes > this.quotas.maxTotalBytes) {
      throw new WorkspacePolicyError("QUOTA_TOTAL", "workspace total byte quota exceeded");
    }
  }

  async writeText(relative, content) {
    if (typeof content !== "string") {
      throw new WorkspacePolicyError("ARG_INVALID", "content must be a string");
    }
    const target = await this.resolveTarget(relative, { create: true });
    const bytes = Buffer.byteLength(content, "utf8");
    await this.enforceWriteQuota(relative, bytes);
    await mkdir(path.dirname(target), { recursive: true });
    await writeFile(target, content, { encoding: "utf8", flag: "w" });
    if (!this.files.has(relative)) {
      if (this.files.size >= this.quotas.maxFiles) {
        throw new WorkspacePolicyError("QUOTA_FILES", "workspace file count quota exceeded");
      }
      this.files.add(relative);
    }
    this.totalBytes += bytes;
    return { bytes };
  }

  async readText(relative) {
    const target = await this.resolveTarget(relative, { mustExist: true });
    const data = await readFile(target);
    if (data.byteLength > this.quotas.maxFileBytes) {
      throw new WorkspacePolicyError("QUOTA_FILE", "file exceeds per-file read quota");
    }
    return data.toString("utf8");
  }

  async list(prefix = "") {
    const prefixPath = prefix ? safeRelative(prefix) : "";
    const root = prefixPath ? path.join(this.root, prefixPath) : this.root;
    const found = [];
    const walk = async (directory, depth) => {
      if (depth > 16) {
        return;
      }
      const entries = await readdir(directory, { withFileTypes: true });
      for (const entry of entries) {
        const full = path.join(directory, entry.name);
        if (entry.isSymbolicLink()) {
          continue;
        }
        if (entry.isDirectory()) {
          await walk(full, depth + 1);
        } else if (entry.isFile()) {
          found.push(path.relative(this.root, full).split(path.sep).join("/"));
        }
        if (found.length >= 500) {
          return;
        }
      }
    };
    await walk(root, 0);
    return found.sort();
  }
}

export function buildWorkspaceTools(workspaceRoot, quotas) {
  const workspace = new Workspace(workspaceRoot, quotas);

  const readTool = {
    name: "read_file",
    label: "read a file from the workspace",
    description: "Read a UTF-8 text file relative to the workspace root.",
    parameters: {
      type: "object",
      properties: { path: { type: "string" } },
      required: ["path"],
      additionalProperties: false,
    },
    async execute(toolCallId, params) {
      const text = await workspace.readText(params.path);
      return { content: [{ type: "text", text }], details: { path: params.path } };
    },
  };

  const writeTool = {
    name: "write_file",
    label: "write a file into the workspace",
    description: "Write (or overwrite) a UTF-8 text file relative to the workspace root.",
    parameters: {
      type: "object",
      properties: {
        path: { type: "string" },
        content: { type: "string" },
      },
      required: ["path", "content"],
      additionalProperties: false,
    },
    async execute(toolCallId, params) {
      const { bytes } = await workspace.writeText(params.path, params.content);
      return {
        content: [{ type: "text", text: `wrote ${bytes} bytes to ${params.path}` }],
        details: { path: params.path, bytes },
      };
    },
  };

  const listTool = {
    name: "list_files",
    label: "list workspace files",
    description: "List files under the workspace root (optionally under a prefix).",
    parameters: {
      type: "object",
      properties: { prefix: { type: "string" } },
      additionalProperties: false,
    },
    async execute(toolCallId, params) {
      const files = await workspace.list(params?.prefix ?? "");
      return {
        content: [{ type: "text", text: files.length ? files.join("\n") : "(empty)" }],
        details: { count: files.length },
      };
    },
  };

  const byName = new Map([
    [readTool.name, readTool],
    [writeTool.name, writeTool],
    [listTool.name, listTool],
  ]);

  return {
    workspace,
    tools: byName,
    select(names) {
      const selected = [];
      for (const name of names) {
        const tool = byName.get(name);
        if (!tool) {
          throw new WorkspacePolicyError("TOOL_UNKNOWN", `unknown workspace tool: ${name}`);
        }
        selected.push(tool);
      }
      return selected;
    },
  };
}
