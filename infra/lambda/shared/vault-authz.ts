type AccessLevel = "viewer" | "editor" | "owner";

const ROLE_SETS: Record<AccessLevel, readonly string[]> = {
  viewer: ["viewer", "editor", "owner"],
  editor: ["editor", "owner"],
  owner: ["owner"],
};

function roleList(level: AccessLevel): string {
  return ROLE_SETS[level].map((role) => `'${role}'`).join(", ");
}

export function entryAccessPredicate(
  entryAlias: string,
  userParam: number,
  level: AccessLevel,
): string {
  const roles = roleList(level);
  const user = `$${userParam}`;

  return `(
    ${entryAlias}.user_id = ${user}
    OR COALESCE(${entryAlias}.owner_user_id, ${entryAlias}.user_id) = ${user}
    OR EXISTS (
      SELECT 1
        FROM vault.entry_access ea
       WHERE ea.entry_id = ${entryAlias}.id
         AND ea.role IN (${roles})
         AND (
           (ea.subject_type = 'user' AND ea.subject_id = ${user})
           OR (
             ea.subject_type = 'organization'
             AND EXISTS (
               SELECT 1
                 FROM vault.organization_members om
                WHERE om.organization_id = ea.subject_id
                  AND om.user_id = ${user}
                  AND om.role IN (${roles})
             )
           )
           OR (
             ea.subject_type = 'project'
             AND EXISTS (
               SELECT 1
                 FROM vault.project_members pm
                WHERE pm.project_id = ea.subject_id
                  AND pm.user_id = ${user}
                  AND pm.role IN (${roles})
             )
           )
         )
    )
    OR (
      ${entryAlias}.organization_id IS NOT NULL
      AND EXISTS (
        SELECT 1
          FROM vault.organization_members om
         WHERE om.organization_id = ${entryAlias}.organization_id
           AND om.user_id = ${user}
           AND om.role IN (${roles})
      )
    )
    OR (
      ${entryAlias}.project_id IS NOT NULL
      AND EXISTS (
        SELECT 1
          FROM vault.project_members pm
         WHERE pm.project_id = ${entryAlias}.project_id
           AND pm.user_id = ${user}
           AND pm.role IN (${roles})
      )
    )
  )`;
}
