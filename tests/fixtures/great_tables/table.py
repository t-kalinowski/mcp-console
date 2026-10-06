import pandas as pd
from great_tables import GT, loc, md, style

table = (
    GT(
        pd.DataFrame(
            {
                "Region": ["North", "South"],
                "Revenue": [1234.5, 9876.0],
                "Share": [0.126, 0.874],
            }
        )
    )
    .tab_header(title=md("**Quarterly sales**"))
    .fmt_currency(columns="Revenue", currency="USD", decimals=2)
    .fmt_percent(columns="Share", decimals=1)
    .tab_style(style=style.fill(color="#e5f5ff"), locations=loc.column_labels())
    .tab_style(style=style.fill(color="#fff2cc"), locations=loc.body(columns="Revenue"))
    .tab_options(table_width="500px")
)
