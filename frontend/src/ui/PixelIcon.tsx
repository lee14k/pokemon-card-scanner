import { PIXEL_ICON_SIZE, pixelPath, type PixelIconName } from "@shared/pixelIcons";

interface Props {
  name: PixelIconName;
  /** Rendered size in px; multiples of 12 stay perfectly crisp. */
  size?: number;
  className?: string;
  title?: string;
}

export default function PixelIcon({ name, size = 24, className, title }: Props) {
  return (
    <svg
      className={`pixel-icon${className ? ` ${className}` : ""}`}
      width={size}
      height={size}
      viewBox={`0 0 ${PIXEL_ICON_SIZE} ${PIXEL_ICON_SIZE}`}
      aria-hidden={title ? undefined : true}
      role={title ? "img" : undefined}
    >
      {title && <title>{title}</title>}
      <path d={pixelPath(name)} fill="currentColor" />
    </svg>
  );
}
