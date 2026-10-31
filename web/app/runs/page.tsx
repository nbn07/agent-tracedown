import { Suspense } from "react";
import { RunsPage } from "@/components/runs/runs-page";
export const metadata = { title: "Runs" };
export default function Page() { return <Suspense><RunsPage /></Suspense>; }
