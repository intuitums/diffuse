"use client";

import { AlertTriangle, RefreshCw } from "lucide-react";

import { Button } from "@/components/ui/button";

export default function ErrorBoundary({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <main className="grid min-h-screen place-items-center bg-[#0c0c11] p-6">
      <section className="w-full max-w-lg rounded-xl border border-white/[0.07] bg-[#13121a] p-8">
        <span className="grid size-10 place-items-center rounded-lg bg-[#f16d67]/10 text-[#f16d67]">
          <AlertTriangle className="size-5" />
        </span>
        <p className="mt-8 text-[9px] font-extrabold uppercase tracking-[0.16em] text-[#f16d67]">
          Control room interrupted
        </p>
        <h1 className="mt-2 text-3xl font-semibold tracking-tight text-[#efeee8]">
          The live view lost its signal.
        </h1>
        <p className="mt-4 text-sm leading-6 text-[#777582]">
          No source data was affected. Retry the reactive connection, then
          inspect the deployment logs if this state persists.
        </p>
        {error.digest ? (
          <p className="mt-3 font-mono text-[9px] text-[#56545f]">
            Reference: {error.digest}
          </p>
        ) : null}
        <Button
          className="mt-6 bg-[#eaff49] text-[#0c0c11] hover:bg-[#f2ff83]"
          onClick={reset}
        >
          <RefreshCw />
          Retry connection
        </Button>
      </section>
    </main>
  );
}
