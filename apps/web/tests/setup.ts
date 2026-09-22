/* 测试环境补丁（第 0 步，REDESIGN-PLAN.md §3.1）
 *
 * happy-dom 不实现 canvas 2D 上下文，而 Semi 的包入口会连带引入 lottie-web，
 * 后者在模块求值阶段就取 2d 上下文并写 fillStyle，导致整包导入在测试里直接崩。
 * 这里补一个最小可用的 2D 上下文与两个浏览器 API，让设计系统能在测试里渲染。
 * 这是测试环境补丁，不是产品代码。 */
type Ctx2D = CanvasRenderingContext2D;

function stubContext2D(canvas: HTMLCanvasElement): Ctx2D {
  const noop = () => undefined;
  const ctx = {
    canvas,
    fillStyle: "",
    strokeStyle: "",
    lineWidth: 1,
    lineCap: "butt",
    lineJoin: "miter",
    globalAlpha: 1,
    globalCompositeOperation: "source-over",
    font: "",
    textAlign: "start",
    textBaseline: "alphabetic",
    save: noop,
    restore: noop,
    scale: noop,
    rotate: noop,
    translate: noop,
    transform: noop,
    setTransform: noop,
    resetTransform: noop,
    clearRect: noop,
    fillRect: noop,
    strokeRect: noop,
    beginPath: noop,
    closePath: noop,
    moveTo: noop,
    lineTo: noop,
    bezierCurveTo: noop,
    quadraticCurveTo: noop,
    arc: noop,
    arcTo: noop,
    rect: noop,
    fill: noop,
    stroke: noop,
    clip: noop,
    drawImage: noop,
    putImageData: noop,
    fillText: noop,
    strokeText: noop,
    setLineDash: noop,
    getLineDash: () => [] as number[],
    createLinearGradient: () => ({ addColorStop: noop }),
    createRadialGradient: () => ({ addColorStop: noop }),
    createPattern: () => null,
    measureText: (text: string) => ({ width: text.length * 6 }) as TextMetrics,
    getImageData: () => ({ data: new Uint8ClampedArray(4), width: 1, height: 1 }) as ImageData,
    createImageData: () => ({ data: new Uint8ClampedArray(4), width: 1, height: 1 }) as ImageData,
    isPointInPath: () => false,
  };
  return ctx as unknown as Ctx2D;
}

if (typeof HTMLCanvasElement !== "undefined") {
  const original = HTMLCanvasElement.prototype.getContext;
  HTMLCanvasElement.prototype.getContext = function getContext(
    this: HTMLCanvasElement,
    contextId: string,
    ...rest: unknown[]
  ): unknown {
    if (contextId === "2d") return stubContext2D(this);
    return original
      ? (original as (...args: unknown[]) => unknown).call(this, contextId, ...rest)
      : null;
  } as typeof HTMLCanvasElement.prototype.getContext;
}

if (typeof globalThis.ResizeObserver === "undefined") {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
}

if (typeof window !== "undefined" && typeof window.matchMedia !== "function") {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener() {},
    removeListener() {},
    addEventListener() {},
    removeEventListener() {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
}
