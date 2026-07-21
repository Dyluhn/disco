create table if not exists todo (
  id integer primary key autoincrement,
  title text not null,
  done integer not null default 0
);
