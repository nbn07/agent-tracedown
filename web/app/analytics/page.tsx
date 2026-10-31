import { Suspense } from "react";
import { AnalyticsPage } from "@/components/analytics/analytics";
export const metadata = { title: "Analytics" };
export default function Page() { return <Suspense><AnalyticsPage /></Suspense>; }
