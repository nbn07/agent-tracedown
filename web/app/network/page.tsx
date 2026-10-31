import { Suspense } from "react";
import { NetworkPage } from "@/components/network/network-page";
export const metadata = { title: "Agent network" };
export default function Page() { return <Suspense><NetworkPage /></Suspense>; }
