import { IncidentDetail } from "@/components/incidents/incidents";
export const metadata = { title: "Incident" };
// One prerendered page serves every id in the static build (see lib/route.ts).
export function generateStaticParams() { return [{ id: "_" }]; }
export default function Page() { return <IncidentDetail />; }
