import { execSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

/* Semi 2.103 的 package.json exports 没有导出 dist/css/*（实测报错），
 * 而完整主题（含组件样式与 body[theme-mode=dark] 深色块）只在 dist/css/semi.css 里。
 * lib/es/_base/base.css 只有 35KB 变量层、没有组件样式。用别名指到真实文件；
 * 这是唯一的包路径绕行，集中在这里。 */
const semiThemeCss = fileURLToPath(
  new URL("./node_modules/@douyinfe/semi-ui/dist/css/semi.css", import.meta.url),
);

/* 构建标识：界面右下角会渲染它，于是**任何截图都自带"这是哪个构建"的证据**。
 * 起因是这次会话里两次被过时截图误导（preview 服务的是旧 dist）。 */
function buildId(): string {
  const stamp = new Date().toISOString().slice(5, 16).replace(/[-T:]/g, "");
  try {
    const sha = execSync("git rev-parse --short HEAD", { encoding: "utf8" }).trim();
    return sha + "·" + stamp;
  } catch {
    return "nogit·" + stamp;
  }
}
const BUILD_ID = buildId();

/* 依赖分包：单一 1.1MB chunk 让每次部署都整包失效，也让首屏解析只能串行。 */
function manualChunks(id: string): string | undefined {
  if (!id.includes("node_modules")) return undefined;
  if (id.includes("@douyinfe")) return "vendor-semi";
  if (id.includes("phosphor")) return "vendor-icons";
  if (id.includes("@radix-ui")) return "vendor-radix";
  if (id.includes("react-router")) return "vendor-router";
  if (id.includes("/react-dom/") || id.includes("/react/") || id.includes("scheduler")) return "vendor-react";
  return "vendor";
}

export default defineConfig({
  plugins: [react(), tailwindcss()],
  define: {
    __BUILD_ID__: JSON.stringify(BUILD_ID),
  },
  resolve: {
    alias: {
      "@douyinfe/semi-ui/dist/css/semi.css": semiThemeCss,
    },
  },
  build: {
    rollupOptions: {
      output: { manualChunks },
    },
  },
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8000",
    },
  },
  test: {
    environment: "happy-dom",
    include: ["tests/**/*.test.{ts,tsx}"],
    // 设计系统（Semi）需要 canvas / ResizeObserver / matchMedia 补丁，见 tests/setup.ts
    setupFiles: ["tests/setup.ts"],
    // 让 tests/stylesheet.test.ts 的 `?raw` 样式表导入拿到真实 CSS 文本
    css: true,
  },
});
