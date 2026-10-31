import { Suspense } from "react";
import { DiffPage } from "@/components/diff";
export const metadata = { title: "Compare runs" };
export default function Page() { return <Suspense><DiffPage /></Suspense>; }
