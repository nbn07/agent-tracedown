"use client";
import { useEffect, useRef } from "react";
import { useReactFlow, useStoreApi } from "@xyflow/react";

/** Fit a known bounding box into the flow's container, now and whenever it resizes.
 *  (Measuring nodes first is unreliable inside tabs and panels that are still laying out.) */
export function FitBox({ box, pad = 16, maxZoom = 1.1 }: { box: { x: number; y: number; w: number; h: number }; pad?: number; maxZoom?: number }) {
  const { setViewport } = useReactFlow();
  const store = useStoreApi();
  const key = `${box.x},${box.y},${box.w},${box.h}`;
  const last = useRef("");
  useEffect(() => {
    const el = store.getState().domNode;
    if (!el) return;
    const fit = () => {
      const { width, height } = el.getBoundingClientRect();
      if (!width || !height) return;
      const zoom = Math.min(maxZoom, (width - 2 * pad) / box.w, (height - 2 * pad) / box.h);
      const x = (width - box.w * zoom) / 2 - box.x * zoom;
      const y = (height - box.h * zoom) / 2 - box.y * zoom;
      const sig = `${key}|${width}|${height}`;
      if (sig === last.current) return;
      last.current = sig;
      setViewport({ x, y, zoom });
    };
    fit();
    const ro = new ResizeObserver(fit);
    ro.observe(el);
    return () => ro.disconnect();
  }, [key, pad, maxZoom, setViewport, store, box.x, box.y, box.w, box.h]);
  return null;
}
