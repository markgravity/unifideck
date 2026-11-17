/**
 * AnkerGamesBrowseTab — Steam-Store-like browse page for AnkerGames,
 * inside the Quick Access panel.
 *
 * AnkerGames has no ownership model (see
 * `py_modules/unifideck/stores/ankergames/catalog.py`), so its Library
 * tab (`tab-container.ts`) shows the flat 2,219-title catalog with no
 * way to group it. This tab instead renders the site's own curated
 * homepage sections (Spotlight, Trending, Most Downloaded, Updated
 * This Week, Coming Soon, Early Access) as horizontally-scrollable
 * rows — the actual request this component exists to satisfy, which
 * the Library tab's filter-only architecture cannot render (see
 * `QuickAccessPanel.tsx`'s docstring for why this lives here and not
 * there).
 *
 * Each tile installs through the same `install_game` RPC every other
 * install button in this plugin uses (`useDownloads().installGame`),
 * so a successful click lands in the normal download queue / Installed
 * list, not a parallel code path.
 */
import { FC, useState } from "react";
import { Focusable, Spinner } from "@decky/ui";
import { useTranslation } from "react-i18next";
import { useRPCQuery } from "../../api/useRPC";
import { rpcRoutes } from "../../api/rpc-routes";
import { useDownloads } from "../../contexts/DownloadContext";
import { useToast } from "../../hooks/useToast";
import type { ToastHeader, ToastString } from "../../services/toast-text";

interface BrowseGame {
  slug: string;
  title: string;
  poster: string | null;
  size_gb: number | null;
}

type BrowseSections = Record<string, BrowseGame[]>;

/** Display order + label for the sections `/home` hands back. Any section
 *  the backend returns that isn't listed here is skipped rather than shown
 *  with a raw API key as its title — ankergames.net can add a new homepage
 *  section at any time and this tab should degrade quietly, not render
 *  `"coming_soon_v2"` as a row title. */
const SECTION_ORDER: { key: string; labelKey: string }[] = [
  { key: "spotlight", labelKey: "ankergamesBrowse.spotlight" },
  { key: "trending", labelKey: "ankergamesBrowse.trending" },
  { key: "most_downloaded", labelKey: "ankergamesBrowse.mostDownloaded" },
  { key: "updated_this_week", labelKey: "ankergamesBrowse.updatedThisWeek" },
  { key: "early_access", labelKey: "ankergamesBrowse.earlyAccess" },
  { key: "coming_soon", labelKey: "ankergamesBrowse.comingSoon" },
  { key: "game_of_the_day", labelKey: "ankergamesBrowse.gameOfTheDay" },
];

const GameTile: FC<{ game: BrowseGame }> = ({ game }) => {
  const { installGame } = useDownloads();
  const toast = useToast();
  const [installing, setInstalling] = useState(false);

  const onInstall = async (): Promise<void> => {
    if (installing) return;
    setInstalling(true);
    try {
      const result = await installGame("ankergames", game.slug);
      if (result && result.success === false) {
        toast.error(
          game.title as ToastHeader,
          (result.error ?? "") as ToastString,
        );
      }
    } finally {
      setInstalling(false);
    }
  };

  return (
    <Focusable
      onActivate={onInstall}
      style={{
        width: 120,
        flex: "0 0 auto",
        display: "flex",
        flexDirection: "column",
        gap: 4,
      }}
    >
      <div
        style={{
          width: 120,
          height: 160,
          borderRadius: 4,
          overflow: "hidden",
          background: "#1b1f23",
          opacity: installing ? 0.5 : 1,
        }}
      >
        {game.poster && (
          <img
            src={game.poster}
            alt={game.title}
            style={{ width: "100%", height: "100%", objectFit: "cover" }}
          />
        )}
      </div>
      <span
        style={{
          fontSize: "0.8em",
          whiteSpace: "nowrap",
          overflow: "hidden",
          textOverflow: "ellipsis",
        }}
      >
        {installing ? "…" : game.title}
      </span>
    </Focusable>
  );
};

const SectionRow: FC<{ title: string; games: BrowseGame[] }> = ({
  title,
  games,
}) => {
  if (games.length === 0) return null;
  return (
    <div style={{ marginBottom: 16 }}>
      <div style={{ fontWeight: 600, marginBottom: 6, padding: "0 4px" }}>
        {title}
      </div>
      <Focusable
        flow-children="horizontal"
        style={{ display: "flex", gap: 8, overflowX: "auto", padding: "0 4px" }}
      >
        {games.map((g) => (
          <GameTile key={g.slug} game={g} />
        ))}
      </Focusable>
    </div>
  );
};

export const AnkerGamesBrowseTab: FC = () => {
  const { t } = useTranslation();
  const { data, loading, error } = useRPCQuery<[], BrowseSections>(
    rpcRoutes.getAnkerGamesBrowseSections,
    [],
  );

  if (loading && !data) {
    return (
      <div style={{ display: "flex", justifyContent: "center", padding: 24 }}>
        <Spinner />
      </div>
    );
  }
  if (error || !data || Object.keys(data).length === 0) {
    return (
      <div style={{ padding: 16, opacity: 0.7 }}>
        {t("ankergamesBrowse.unavailable")}
      </div>
    );
  }

  return (
    <div>
      {SECTION_ORDER.map(({ key, labelKey }) => (
        <SectionRow key={key} title={t(labelKey)} games={data[key] ?? []} />
      ))}
    </div>
  );
};
