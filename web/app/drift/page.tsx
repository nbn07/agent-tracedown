import { Suspense } from "react";
import { DriftPage } from "@/components/drift/drift";
export const metadata = { title: "Drift" };
export default function Page() { return <Suspense><DriftPage /></Suspense>; }
