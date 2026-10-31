import { RunDetailPage } from "@/components/runs/run-detail";
export const metadata = { title: "Run" };
// One prerendered page serves every id in the static build (see lib/route.ts).
export function generateStaticParams() { return [{ id: "_" }]; }
export default function Page() { return <RunDetailPage />; }
