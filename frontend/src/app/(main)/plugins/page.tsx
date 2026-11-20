"use client";

import { ArrowLeft, Plug } from "lucide-react";
import { useTranslation } from "react-i18next";
import Link from "next/link";
import { Button } from "@/components/ui/button";
import { PluginsTabContent } from "./content";

export default function PluginsPage() {
  const { t } = useTranslation("plugins");

  return (
    <div className="flex-1 overflow-y-auto bg-[var(--surface-chat)] scrollbar-auto">
      <div className="page-shell mx-auto w-full max-w-5xl px-5 pb-10 pt-20 sm:px-8 lg:py-10">
        {/* Header */}
        <div className="page-header mb-8 flex min-h-12 items-center gap-3 border-b border-[var(--border-subtle)] pb-6">
          <Button variant="ghost" size="icon" className="h-8 w-8 lg:hidden" asChild>
            <Link href="/c/new" aria-label={t("settings:backToApp")}>
              <ArrowLeft className="h-4 w-4" />
            </Link>
          </Button>
          <Plug className="h-6 w-6 text-[var(--text-secondary)]" />
          <h1 className="text-[26px] font-semibold tracking-tight text-[var(--text-primary)]">
            {t("title")}
          </h1>
        </div>
        <PluginsTabContent />
      </div>
    </div>
  );
}
