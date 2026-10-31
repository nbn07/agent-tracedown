import { Suspense } from "react";
import { IncidentsPage } from "@/components/incidents/incidents";
export const metadata = { title: "Incidents" };
export default function Page() { return <Suspense><IncidentsPage /></Suspense>; }
