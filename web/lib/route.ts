"use client";
import { useParams, usePathname } from "next/navigation";

// The static build that ships inside the Python package prerenders one page per detail route
// (/runs/_, /incidents/_), and the server answers every /runs/<id> with it. There the route
// params say "_", so the id is read from the address bar instead.
export function useRouteId(): string {
  const params = useParams<{ id: string }>();
  const path = usePathname() || "";
  if (params?.id && params.id !== "_") return params.id;
  return decodeURIComponent(path.split("/").filter(Boolean)[1] || "");
}
