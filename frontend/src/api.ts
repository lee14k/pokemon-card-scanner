// The web app's handle on the shared API client (../shared/src/client.ts).
// Same-origin in production (the API serves this build); the Vite proxy's /api
// prefix in dev; or VITE_API_BASE for a phone pointed at a LAN backend.
import { createApi } from "@shared/client";

export * from "@shared/types";
export { ApiError, isNoCardsFound, type UploadFile } from "@shared/client";

const base =
  import.meta.env.VITE_API_BASE?.replace(/\/$/, "") ||
  (import.meta.env.DEV ? "/api" : "");

export const api = createApi(base);

// The client's methods are closures (no `this`), so they destructure safely.
export const {
  scanPack,
  scanPackStream,
  getPackFollowup,
  getBinderFollowup,
  lookupCard,
  getSets,
  register,
  login,
  logout,
  me,
  savePull,
  listPulls,
  patchPullCode,
  liveStart,
  liveFrame,
  liveState,
  liveCardImageUrl,
  liveDuplicate,
  liveReplace,
  liveFinish,
  statsSets,
  statsSetDetail,
  statsAnomalies,
  updateAnomaly,
  recomputeStats,
  adminTrainers,
  setTrainerRole,
  getDex,
  scanBinder,
  saveToCollection,
  getCollection,
  patchCollectionQty,
  deleteCollectionCard,
  randomBattle,
  botBattle,
  friendBattle,
  acceptBattle,
  declineBattle,
  listBattles,
  battleInbox,
} = api;
