import { NextResponse } from "next/server";

export function middleware() {
  // Cloud authentication is checked by the session boundary and API gateway.
  return NextResponse.next();
}

export const config = { matcher: ["/((?!api|_next|favicon|manifest|robots|.*\\..*).*)"] };
