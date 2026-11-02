"""Первичная схема PostGIS; учётные записи создаются отдельно."""

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    # Static revision: subsequent model changes must create a new migration.
    for statement in (
        """CREATE TABLE categories (
            code text PRIMARY KEY, name text NOT NULL, geometry_type text NOT NULL)""",
        """CREATE TABLE data_sources (
            code text PRIMARY KEY, title text NOT NULL, url text,
            usage_note text NOT NULL, snapshot_date date NOT NULL)""",
        """CREATE TABLE gis_objects (
            id bigint PRIMARY KEY, name text NOT NULL,
            category_code text NOT NULL REFERENCES categories(code),
            geom geometry(Geometry,4326) NOT NULL,
            status text NOT NULL CHECK (status IN ('active','closed','unknown')),
            rating numeric CHECK (rating BETWEEN 1 AND 5),
            capacity integer CHECK (capacity >= 0),
            area_m2 double precision, length_m double precision,
            opening_date date, is_accessible boolean,
            source text NOT NULL REFERENCES data_sources(code), source_id text,
            synthetic_fields text[] NOT NULL, updated_at timestamptz NOT NULL,
            CONSTRAINT object_geometry_valid CHECK (
                ST_IsValid(geom) AND NOT ST_IsEmpty(geom) AND ST_NDims(geom)=2),
            CONSTRAINT object_geometry_type CHECK (
                (category_code IN ('school','hospital','pharmacy') AND
                 GeometryType(geom)='POINT') OR
                (category_code='park' AND GeometryType(geom)='POLYGON') OR
                (category_code='improvement_zone' AND GeometryType(geom)='LINESTRING')),
            CONSTRAINT object_area CHECK (
                (category_code='park' AND area_m2 IS NOT NULL AND area_m2 >= 0
                 AND area_m2 < 'Infinity'::float8) OR
                (category_code<>'park' AND area_m2 IS NULL)),
            CONSTRAINT object_length CHECK (
                (category_code='improvement_zone' AND length_m IS NOT NULL AND length_m >= 0
                 AND length_m < 'Infinity'::float8) OR
                (category_code<>'improvement_zone' AND length_m IS NULL)))""",
        "CREATE INDEX ix_gis_objects_category_code ON gis_objects(category_code)",
        "CREATE INDEX idx_gis_objects_geom ON gis_objects USING gist(geom)",
        """CREATE TABLE regions (
            id bigint PRIMARY KEY, name text NOT NULL UNIQUE,
            geom geometry(Geometry,4326) NOT NULL,
            source text NOT NULL REFERENCES data_sources(code), source_id text,
            CONSTRAINT region_geometry CHECK (
                GeometryType(geom) IN ('POLYGON','MULTIPOLYGON') AND
                ST_IsValid(geom) AND NOT ST_IsEmpty(geom) AND ST_NDims(geom)=2))""",
        "CREATE INDEX idx_regions_geom ON regions USING gist(geom)",
        """CREATE TABLE users (
            id uuid PRIMARY KEY, login text NOT NULL UNIQUE, password_hash text NOT NULL,
            role text NOT NULL CHECK (role IN ('user','admin')), active boolean NOT NULL,
            created_at timestamptz NOT NULL)""",
        """CREATE TABLE auth_sessions (
            token_hash varchar(64) PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id),
            created_at timestamptz NOT NULL, expires_at timestamptz NOT NULL,
            revoked_at timestamptz, csrf_token text NOT NULL)""",
        "CREATE INDEX ix_auth_sessions_expires_at ON auth_sessions(expires_at)",
        "CREATE INDEX ix_auth_sessions_user_id ON auth_sessions(user_id)",
        """CREATE TABLE dataset_state (
            id integer PRIMARY KEY CHECK(id=1), version bigint NOT NULL CHECK(version>=1),
            changed_at timestamptz NOT NULL)""",
        """CREATE TABLE layer_state (
            layer_code text PRIMARY KEY, imported_at timestamptz, version bigint NOT NULL)""",
        """INSERT INTO categories VALUES
            ('school','Образование','Point'), ('hospital','Здравоохранение','Point'),
            ('pharmacy','Фармация','Point'), ('park','Рекреация','Polygon'),
            ('improvement_zone','Инфраструктура','LineString')""",
        "INSERT INTO dataset_state VALUES (1,1,now())",
        """INSERT INTO layer_state SELECT code,NULL::timestamptz,1 FROM categories
            UNION ALL SELECT 'regions',NULL::timestamptz,1""",
        """CREATE FUNCTION calculate_object_metrics() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                NEW.area_m2 := CASE WHEN GeometryType(NEW.geom)='POLYGON'
                    THEN ST_Area(NEW.geom::geography) ELSE NULL END;
                NEW.length_m := CASE WHEN GeometryType(NEW.geom)='LINESTRING'
                    THEN ST_Length(NEW.geom::geography) ELSE NULL END;
                RETURN NEW;
            END $$""",
        """CREATE TRIGGER object_metrics BEFORE INSERT OR UPDATE ON gis_objects
            FOR EACH ROW EXECUTE FUNCTION calculate_object_metrics()""",
    ):
        op.execute(statement)


def downgrade() -> None:
    for table in (
        "auth_sessions",
        "users",
        "layer_state",
        "dataset_state",
        "regions",
        "gis_objects",
        "data_sources",
        "categories",
    ):
        op.execute(f"DROP TABLE {table}")
    op.execute("DROP FUNCTION calculate_object_metrics()")
