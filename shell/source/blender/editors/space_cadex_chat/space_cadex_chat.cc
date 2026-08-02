/* SPDX-FileCopyrightText: 2026 Mesh Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup spcadexchat
 *
 * The Cadex chat editor: full-height Open Grok terminal.
 *
 * Two regions:
 *
 * - #RGN_TYPE_WINDOW  terminal paint (mesh_agent draw handler).
 * - #RGN_TYPE_HEADER  editor switcher, section icons, Cadex Chat title.
 *
 * No EXECUTE footer: section toggles and restart live in the header.
 * Older screens that still serialize an EXECUTE region are hidden on init.
 */

#include "BLI_listbase.hh"
#include "BLI_string.hh"

#include "BKE_screen.hh"

#include "ED_screen.hh"
#include "ED_space_api.hh"

#include "DNA_screen_types.h"
#include "DNA_space_types.h"

#include "MEM_guardedalloc.h"

#include "WM_api.hh"
#include "WM_types.hh"

#include "UI_interface.hh"
#include "UI_view2d.hh"

#include "BLO_read_write.hh"

namespace blender {

static SpaceLink *cadex_chat_create(const ScrArea * /*area*/, const Scene * /*scene*/)
{
  SpaceCadexChat *chat_space = MEM_new<SpaceCadexChat>("cadex chat space");
  chat_space->spacetype = SPACE_CADEX_CHAT;

  {
    /* Header. */
    ARegion *region = BKE_area_region_new();
    BLI_addtail(&chat_space->regionbase, region);
    region->regiontype = RGN_TYPE_HEADER;
    region->alignment = RGN_ALIGN_TOP;
  }

  {
    /* Main: Open Grok terminal (full remaining height). */
    ARegion *region = BKE_area_region_new();
    BLI_addtail(&chat_space->regionbase, region);
    region->regiontype = RGN_TYPE_WINDOW;
  }

  return (SpaceLink *)chat_space;
}

static void cadex_chat_free(SpaceLink * /*sl*/) {}

static void cadex_chat_init(wmWindowManager * /*wm*/, ScrArea *area)
{
  /* Drop legacy footers so older .blend layouts go full-height. */
  for (ARegion *region = static_cast<ARegion *>(area->regionbase.first); region != nullptr;
       region = region->next)
  {
    if (region->regiontype == RGN_TYPE_EXECUTE) {
      region->flag |= RGN_FLAG_HIDDEN;
    }
  }
}

static SpaceLink *cadex_chat_duplicate(SpaceLink *sl)
{
  SpaceCadexChat *space_chat = MEM_dupalloc(reinterpret_cast<SpaceCadexChat *>(sl));

  return (SpaceLink *)space_chat;
}

static void cadex_chat_blend_write(BlendWriter *writer, SpaceLink *sl)
{
  writer->write_struct_cast<SpaceCadexChat>(sl);
}

static void cadex_chat_operatortypes() {}

static void cadex_chat_keymap(wmKeyConfig * /*keyconf*/) {}

/* -------------------------------------------------------------------- */
/** \name Main Region (terminal)
 * \{ */

static void cadex_chat_main_region_init(wmWindowManager *wm, ARegion *region)
{
  region->v2d.scroll = V2D_SCROLL_RIGHT | V2D_SCROLL_VERTICAL_HIDE;

  ED_region_panels_init(wm, region);
}

static void cadex_chat_main_region_listener(const wmRegionListenerParams *params)
{
  ARegion *region = params->region;
  const wmNotifier *wmn = params->notifier;

  switch (wmn->category) {
    case NC_SCENE:
    case NC_SPACE:
    case NC_WM:
      ED_region_tag_redraw(region);
      break;
  }
}

/** \} */

/* -------------------------------------------------------------------- */
/** \name Header Region
 * \{ */

static void cadex_chat_header_region_init(wmWindowManager * /*wm*/, ARegion *region)
{
  ED_region_header_init(region);
}

static void cadex_chat_header_region_draw(const bContext *C, ARegion *region)
{
  ED_region_header(C, region);
}

/** \} */

void ED_spacetype_cadex_chat()
{
  std::unique_ptr<SpaceType> st = std::make_unique<SpaceType>();
  ARegionType *art;

  st->spaceid = SPACE_CADEX_CHAT;
  STRNCPY(st->name, "Cadex Chat");

  st->create = cadex_chat_create;
  st->free = cadex_chat_free;
  st->init = cadex_chat_init;
  st->duplicate = cadex_chat_duplicate;
  st->operatortypes = cadex_chat_operatortypes;
  st->keymap = cadex_chat_keymap;
  st->blend_write = cadex_chat_blend_write;

  /* regions: main window (terminal) */
  art = MEM_new_zeroed<ARegionType>("spacetype cadex chat region");
  art->regionid = RGN_TYPE_WINDOW;
  art->init = cadex_chat_main_region_init;
  art->layout = ED_region_panels_layout;
  art->draw = ED_region_panels_draw;
  art->listener = cadex_chat_main_region_listener;
  art->keymapflag = ED_KEYMAP_UI;

  BLI_addhead(&st->regiontypes, art);

  /* regions: header */
  art = MEM_new_zeroed<ARegionType>("spacetype cadex chat region");
  art->regionid = RGN_TYPE_HEADER;
  art->prefsizey = HEADERY;
  art->keymapflag = ED_KEYMAP_UI | ED_KEYMAP_VIEW2D | ED_KEYMAP_HEADER;
  art->init = cadex_chat_header_region_init;
  art->draw = cadex_chat_header_region_draw;

  BLI_addhead(&st->regiontypes, art);

  /* Legacy EXECUTE type kept registered so old screens load; new areas never
   * create one, and #cadex_chat_init hides any that remain. */
  art = MEM_new_zeroed<ARegionType>("spacetype cadex chat region");
  art->regionid = RGN_TYPE_EXECUTE;
  art->prefsizey = 0;
  art->init = ED_region_panels_init;
  art->layout = ED_region_panels_layout;
  art->draw = ED_region_panels_draw;
  art->listener = cadex_chat_main_region_listener;
  art->keymapflag = ED_KEYMAP_UI;

  BLI_addhead(&st->regiontypes, art);

  BKE_spacetype_register(std::move(st));
}

}  // namespace blender
