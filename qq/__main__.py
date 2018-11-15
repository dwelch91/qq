import sys
import argparse
import csv
import sqlite3

from prettytable import PrettyTable

from qq.exceptions import Error, FilterError
from qq.filters import FILTERS, print_filter_list_table, preprocess_filters
from qq.sql import rewrite_sql, process_table_remapping, process_column_remapping
from qq.utils import error

DEBUG = False


def debug(s):
    if DEBUG:
        sys.stderr.write(f"{s}\n")


def main(args=None):
    global DEBUG
    if args is None:
        args = sys.argv[1:]
    parser = argparse.ArgumentParser()
    parser.add_argument('sql', nargs='*', help="The SQL to execute. "
                                               "Use filenames surrounded by single or double quotes to specify CSV sources instead of existing tables in the FROM clause(s). "
                                               "You can use [:...:] replacements for special characters (see --help-filters for more information.")
    parser.add_argument('--dialect', '-t', choices=csv.list_dialects(), default='unix',
                        help=f"Specify the CSV dialect. Valid values are {', '.join(csv.list_dialects())}. Default is `unix`.")
    parser.add_argument('--delimiter', '-d', default=',', help="Specify the CSV delimiter to use. Default is a comma (,).")
    parser.add_argument('--quotechar', '--quote-char', '-q', default='"', help='Specify the CSV quote charactor. Default is double quote (").')
    parser.add_argument('--output', '-o', default='-', help="Output file. Default is stdout (-).")
    parser.add_argument('--output-format', '--out-format', '--out-fmt', '-f', default='table', choices=['table', 'csv'],
                        help="Output format. Valid value are 'table' and 'csv'. Default is table.")
    parser.add_argument('--save-db', '-s', help="Specify a SQLite database to use (instead of using an in-memory database. The database will remain after qq exits.")
    #parser.add_argument('--load-db', '-l', help="Load an existing database instead of creating a new one.")
    parser.add_argument('--skip-lines', '--skip', '-k', type=int, default=0, help="Skip `SKIP_LINES` lines at the beginning of the file. Default is 0.")
    parser.add_argument('--headers', '-r',
                        help="Don't use the first non-skipped line for header/column names, use these header/column names instead. "
                             "Format is a comma separated list of column names. "
                             "Column names must not be SQLite reserved words.")
    parser.add_argument('--debug', '-g', action='store_true', help="Turn on debug output.")
    parser.add_argument('--filter', '-e', action='append',
                        help="Specify a column filter. Use one filter per switch/param. "
                             "Format is <column_name>|filter|<0 or more params or additional filters in filter chain>.  "
                             "Filters have a variable number of parameters (0+). Filters may be chained.")
    #parser.add_argument('--auto-filter', '-a', action='store_true', help="Automatically apply the `num` filter to all column data.")
    parser.add_argument('--filter-list', '--help-filters', action='store_true')
    parser.add_argument('--remap-column', '--remap-header', '-m', action='append',
                        help="A single column re-map in the form <col_name>=<new_col_name>. Use one switch for each column re-mapping. "
                             "This overrides any column/header names that are auto-discovered or passed in via --headers/-r. "
                             "You can use [:...:] replacements for special characters (see --help-filters for more information.")
    parser.add_argument('--remap-table', '--remap-file', '-l', action='append',
                        help="A single table re-map in the form <table_name>=<new_table_name>. Use one switch for each table re-mapping. "
                             "This overrides any table names that are auto-generated from filenames passed in via the SQL statement. "
                             "You can use [:...:] replacements for special characters (see --help-filters for more information.")

    # TODO: Subparsers? qq query ... qq insert ... qq delete ... qq filter-list ... etc.  Can a subparser be default? (problematic with SQL positional param)
    # TODO: Handle more CSV parser params
    # TODO: Handle duplicate column names (in -r)
    # TODO: Modification queries? (read CSV, apply filters, save to db, apply SQL modification(s), output new CSV)
    # TODO: Auto filtering to number with a switch? (only for columns w/o an explicit filter with -e)

    args = parser.parse_args(args=args)
    DEBUG = args.debug
    debug(args)

    if args.filter_list:
        print_filter_list_table()
        return 0

    if not args.sql:
        raise Error("You must specify the SQL to execute.")

    # Process table re-mappings, if any
    table_remapping = process_table_remapping(args.remap_table)
    debug(table_remapping)

    # Re-write the SQL, replacing filenames with table names and apply table re-mapping(s)
    sql, tables = rewrite_sql(args.sql, table_remapping)
    debug(sql)
    debug(tables)

    # Pre-process the filters
    filters = preprocess_filters(args.filter)
    debug(filters)

    # Process the column re-mappings, if any
    column_remapping = process_column_remapping(args.remap_column)
    debug(column_remapping)

    # TODO: Allow for database "re-use" - open an existing database file and use other tables in it for JOINs, etc along with the CSV input table(s)
    # TODO: --load-db <database name>
    # TODO: Have to handle case where db has an existing table with same name as one of the CSV input table(s)
    if args.save_db:
        con = sqlite3.connect(args.save_db)
    else:
        con = sqlite3.connect(":memory:")  # TODO: will be used if -l or -s not specified

    cur = con.cursor()

    # Read each CSV or TSV file and insert into a SQLite table based on the filename of the file
    for tablename, path in tables.items():
        with open(path) as f:
            if args.skip_lines:
                [f.readline() for _ in range(args.skip_lines)]

            reader = csv.reader(f, dialect=args.dialect, delimiter=args.delimiter, quotechar=args.quotechar)
            first, colnames = True, []

            for row in reader:
                row = [n.strip() for n in row if n]

                if first:
                    placeholders = ', '.join(['?'] * len(row))
                    col_src = args.headers.split(',') if args.headers else row
                    colnames = [column_remapping.get(n.strip()) or n.strip() for n in col_src]
                    colnames_str = ','.join(colnames)

                    s = f"CREATE TABLE {tablename} ({colnames_str});"
                    debug(s)
                    cur.execute(s)
                    first = False
                    continue

                # Process data based on filter chains
                # TODO: Move this section to filters.py
                new_row = []
                if filters:
                    for col, data in zip(colnames, row):
                        if col in filters:
                            params = filters[col][:]
                            while params:
                                filter_name = params.pop(0)
                                if filter_name not in FILTERS:
                                    raise FilterError(f"Error: Invalid filter name: {filter_name}")

                                func, num_params = FILTERS[filter_name][:2]
                                func_args = [params.pop(0) for _ in range(num_params)]
                                data = func(data, *func_args)

                        new_row.append(data)

                    debug(new_row)
                    s = f"INSERT INTO {tablename} ({colnames_str}) VALUES ({placeholders});"
                    cur.execute(s, new_row)

                else:
                    debug(row)
                    s = f"INSERT INTO {tablename} ({colnames_str}) VALUES ({placeholders});"
                    cur.execute(s, row)

    con.commit()

    # TODO: Move to output.py
    debug(sql)
    result = cur.execute(sql)
    column_names = [x[0] for x in cur.description]

    if args.output == '-':
        if args.output_format == 'table':
            table = PrettyTable(column_names)
            table.align = 'l'
            for row in result:
                table.add_row(row)
            print(table)

        elif args.output_format == 'csv':
            writer = csv.writer(sys.stdout, delimiter=args.delimiter)
            writer.writerow(column_names)
            for row in result:
                writer.writerow(row)
    else:
        with open(args.output, 'w', newline='') as f:
            writer = csv.writer(f, delimiter=args.delimiter)
            for row in result:
                writer.writerow(row)

    con.close()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Error as e:
        error(e)
        sys.exit(1)  # TODO: correct result code