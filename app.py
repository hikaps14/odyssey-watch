# ==============================================================================
# PROJECT ODYSSEY-WATCH: STREAMLIT DASHBOARD (MOBILE & WEB RESPONSIVE)
# ==============================================================================
# File: app.py
# Description: Mobile-optimized Streamlit application reading cached seat maps.
#              Provides compact touch-cards for smartphones and full data tables
#              for desktop web browsers.
# ==============================================================================

import json
import os
import streamlit as st
import pandas as pd

# ------------------------------------------------------------------------------
# 1. PAGE CONFIGURATION & CACHE INITIALIZATION
# ------------------------------------------------------------------------------

st.set_page_config(
    page_title="Odyssey IMAX 70mm Tracker",
    page_icon="🎬",
    layout="wide",
    initial_sidebar_state="collapsed"  # Collapsed by default so phones lead with content
)

CACHE_FILE_PATH = "cache_inventory.json"


@st.cache_data(ttl=60)
def load_cache() -> dict:
    """Reads JSON cache snapshot with 60-second in-memory caching."""
    if os.path.exists(CACHE_FILE_PATH):
        try:
            with open(CACHE_FILE_PATH, 'r') as f:
                return json.load(f)
        except Exception:
            return None
    return None


cache_data = load_cache()

# ------------------------------------------------------------------------------
# 2. DASHBOARD HEADER & STATUS BADGE
# ------------------------------------------------------------------------------

st.title("🎬 Odyssey IMAX 70mm Tracker")
st.caption("Cinemark Dallas XD & IMAX (Theater #207) • Real-Time Cached Intelligence")

if not cache_data:
    st.error("⚠️ No scan cache found. Please run `scanner_worker.py` first to initialize the database.")
    st.stop()

# Last Refresh Banner
st.info(f"⚡ **Last Cloud Refresh:** {cache_data.get('last_updated_display', 'Unknown')} (Scans run on schedule)")


# ------------------------------------------------------------------------------
# 3. RESPONSIVE FILTER CONTROLS (TOP EXPANDER FOR MOBILE + SIDEBAR FOR DESKTOP)
# ------------------------------------------------------------------------------

blocks_avail = cache_data.get("blocks_scanned", [1, 2, 3])
block_labels = {
    1: "Block 1 (Aug 07 - Aug 20)",
    2: "Block 2 (Aug 21 - Sep 03)",
    3: "Block 3 (Sep 04 - Sep 16)"
}
days_options = ["Friday", "Saturday", "Sunday", "Monday", "Thursday"]

# Render Top Expander (Ideal for thumb-navigation on mobile phones)
with st.expander("🔍 **Filters & Search Settings (Tap to Expand)**", expanded=False):
    f_col1, f_col2 = st.columns(2)
    
    with f_col1:
        mobile_blocks = st.multiselect(
            "📅 Date Blocks",
            options=blocks_avail,
            default=blocks_avail,
            format_func=lambda x: block_labels.get(x, f"Block {x}"),
            key="mobile_blocks"
        )
        mobile_days = st.multiselect(
            "📆 Days of Week",
            options=days_options,
            default=["Friday", "Saturday", "Sunday"],
            key="mobile_days"
        )
        
    with f_col2:
        mobile_row_pref = st.radio(
            "💺 Row Quality",
            options=[
                "All Open Showtimes",
                "Prime Back Rows Only (Rows F–L)",
                "Mid & Front Rows Only (Rows A–E)"
            ],
            index=0,
            key="mobile_row_pref"
        )
        mobile_pairs_only = st.checkbox("👫 Require Consecutive Pairs", value=False, key="mobile_pairs")

# Sync Desktop Sidebar Filters with Top Mobile Filters
st.sidebar.header("🎯 Desktop Filter Panel")
selected_blocks = mobile_blocks
selected_days = mobile_days
row_preference = mobile_row_pref
pairs_only = mobile_pairs_only

min_seats = st.sidebar.slider("🔢 Min Open Standard Seats", min_value=1, max_value=20, value=1)


# ------------------------------------------------------------------------------
# 4. DATA FILTERING ENGINE
# ------------------------------------------------------------------------------

all_shows = cache_data.get("showtimes", [])
filtered_shows = []

for show in all_shows:
    # Filter 1: Date Block
    if show["block"] not in selected_blocks:
        continue
        
    # Filter 2: Day of Week
    if show["day_name"] not in selected_days:
        continue
        
    # Filter 3: Minimum Open Count
    if show["total_std_open"] < min_seats:
        continue
        
    # Filter 4: Row Quality Tier
    has_back_rows = any(r in show["rows_breakdown"] for r in ['F', 'G', 'H', 'J', 'K', 'L'])
    if row_preference == "Prime Back Rows Only (Rows F–L)" and not has_back_rows:
        continue
    elif row_preference == "Mid & Front Rows Only (Rows A–E)" and has_back_rows:
        continue
        
    # Filter 5: Pair Availability
    if pairs_only:
        total_pairs = sum(info["pairs"] for info in show["rows_breakdown"].values())
        if total_pairs == 0:
            continue
            
    filtered_shows.append(show)


# ------------------------------------------------------------------------------
# 5. METRIC CARDS & DISPLAY MODE TOGGLE
# ------------------------------------------------------------------------------

col1, col2, col3 = st.columns(3)
col1.metric("Matching Shows", len(filtered_shows))
col2.metric("Total Open Seats", sum(s["total_std_open"] for s in filtered_shows))
col3.metric("Back-Row Pairs", sum(1 for s in filtered_shows if s["has_back_row_pair"]))

st.markdown("---")

# Layout Toggle: Allows switching between Mobile-Friendly Cards and Desktop Table
view_mode = st.radio(
    "📱 **Display Mode:**", 
    options=["Mobile Touch Cards", "Desktop Data Table"], 
    horizontal=True
)

# ------------------------------------------------------------------------------
# 6. RENDER LOGIC (TOUCH CARDS vs DATA TABLE)
# ------------------------------------------------------------------------------

if not filtered_shows:
    st.warning("🔒 No showtimes match your exact filter criteria. Try adjusting the filters above.")
else:
    # MODE A: Mobile Touch Cards (Default - High touch targets for mobile screens)
    if view_mode == "Mobile Touch Cards":
        for s in filtered_shows:
            with st.container():
                st.subheader(f"📅 {s['date_display']} at {s['time_display']}")
                
                c_m1, c_m2 = st.columns(2)
                c_m1.write(f"**Day:** {s['day_name']}")
                c_m1.write(f"**Open Seats:** {s['total_std_open']} standard")
                c_m2.write(f"**Best Row:** {s['best_row_desc']}")
                c_m2.write(f"**Pairs:** {'YES 🎯' if s['has_back_row_pair'] else 'None'}")
                
                # Large, thumb-friendly tap button
                st.link_button(
                    label=f"🎟️ Book Seats for {s['time_display']} Showtime", 
                    url=s['direct_booking_url'], 
                    use_container_width=True
                )
                st.divider()

    # MODE B: Desktop Data Table (Dense comparison spreadsheet view)
    else:
        table_rows = []
        for s in filtered_shows:
            table_rows.append({
                "Date": s["date_display"],
                "Time": s["time_display"],
                "Day": s["day_name"],
                "Best Standard Row": s["best_row_desc"],
                "Open Std Seats": f"{s['total_std_open']} seats",
                "Back-Row Pairs": "YES 🎯" if s["has_back_row_pair"] else "None",
                "Booking Link": s["direct_booking_url"]
            })
            
        df = pd.DataFrame(table_rows)
        
        st.dataframe(
            df,
            column_config={
                "Booking Link": st.column_config.LinkColumn(
                    "Direct Booking Link",
                    display_text="Book Seats On Cinemark"
                )
            },
            use_container_width=True,
            hide_index=True
        )

# ------------------------------------------------------------------------------
# 7. ROW-BY-ROW DRILL DOWN EXPANDER
# ------------------------------------------------------------------------------

st.markdown("---")
st.subheader("🔍 Detailed Row-by-Row Seat Breakdown")

for s in filtered_shows:
    with st.expander(f"📅 {s['date_display']} at {s['time_display']} ({s['day_name']}) — {s['total_std_open']} Standard Seats Open"):
        st.write(f"**Direct Seat Map Link:** [{s['direct_booking_url']}]({s['direct_booking_url']})")
        st.write(f"**Best Row Available:** {s['best_row_desc']}")
        
        rows_data = s["rows_breakdown"]
        if rows_data:
            row_list = []
            for r_name, r_info in rows_data.items():
                type_str = "PREFERRED BACK" if r_info["is_back_row"] else "FRONT/MID"
                row_list.append({
                    "Row": f"Row {r_name}",
                    "Tier": type_str,
                    "Open Seats": r_info["open_count"],
                    "Pairs": r_info["pairs"],
                    "Available Seats": str(r_info["seats"])
                })
            st.table(pd.DataFrame(row_list))
        else:
            st.write("No standard seats available for this showtime.")
