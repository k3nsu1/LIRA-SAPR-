# Result query arguments

Query arguments (all result tools): components - which values (default all);
sort_by - a component to order rows by, by absolute value unless by_abs=false;
top - rows to return (at least 1, default 20, null = up to 500); envelope=true - per
place (node, or element and section) the minimum and maximum of each component over
load cases / combinations / shapes / limit states, with where each occurs (sorting and
threshold then use the one larger in magnitude); threshold - keep only rows whose
sort_by value reaches it, in the units of the answer (by absolute value unless
by_abs=false); export_path - absolute .csv or .xlsx path: the whole table after
envelope and threshold is written there (units in column headers; an existing file is
replaced) and the answer names the file. The answer always has the extremes of each
component with their place, and total_rows. nodes / elements also take "selected" or
"visible" (what the user selected in LIRA's window, or what LIRA shows after a
fragmentation): the model must then be open in LIRA, and LIRA closes it after the
analysis, so reopen it first.
