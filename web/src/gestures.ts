export type GestureAction = "recommended" | "rejected" | "abstained" | "alternatives" | null;

export function gestureAction(dx: number, dy: number, threshold = 82): GestureAction {
  if (Math.abs(dx) < threshold && Math.abs(dy) < threshold) return null;
  if (dy < -threshold && Math.abs(dy) > Math.abs(dx) * 1.1) return "abstained";
  if (dy > threshold && Math.abs(dy) > Math.abs(dx) * 1.1) return "alternatives";
  if (dx > threshold) return "recommended";
  if (dx < -threshold) return "rejected";
  return null;
}
