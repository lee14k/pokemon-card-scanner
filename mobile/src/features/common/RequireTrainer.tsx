import type { ReactNode } from "react";
import { router } from "expo-router";
import type { PixelIconName } from "@shared/pixelIcons";
import { useAuth } from "@/lib/auth";
import { EmptyState, LoadingBlock, PixelButton } from "@/ui/kit";

/** Renders children for a logged-in trainer, or a friendly login prompt. */
export function RequireTrainer({
  icon,
  reason,
  children,
}: {
  icon: PixelIconName;
  reason: string;
  children: ReactNode;
}) {
  const { trainer, loading } = useAuth();
  if (loading) return <LoadingBlock label="Checking your trainer card…" />;
  if (!trainer)
    return (
      <EmptyState
        icon={icon}
        title="Trainers only"
        body={reason}
        action={
          <PixelButton
            title="Log in or sign up"
            variant="primary"
            onPress={() => router.push({ pathname: "/login", params: { reason } })}
          />
        }
      />
    );
  return <>{children}</>;
}
