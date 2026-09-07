#include "mini.h"

static int external_cleanup(void)
{
    MiniNode *node = cJSON_New_Item();
    cJSON_Delete(node);
    return 1;
}

int workflow(void)
{
    const char *end = 0;
    MiniNode *parsed = cJSON_ParseWithOpts("{", &end, 0);
    cJSON_Delete(parsed);
    return external_cleanup();
}

int mini_entrypoint(void)
{
    return workflow() + run_all();
}
