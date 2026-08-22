(function () {
    "use strict";

    console.log("MAIN CONTROLLER START");


    async function safeRequest(method, params) {
        try {
            const result =
                await VmbackupApi.request(
                    method,
                    params
                );

            console.log("RPC OK", method, result);
            return result;

        } catch (e) {
            console.error(
                "RPC FAILED",
                method,
                e
            );

            return [];
        }
    }

    async function start() {
        console.log("MAIN START");

        const status =
            await VmbackupApi.request(
                "daemon.status"
            );

        console.log(
            "STATUS",
            status
        );

        const [
            inventory,
            registeredVms,
            storage,
            jobs,
            runPage,
            recovery
        ] = await Promise.all([
            safeRequest("vm.inventory"),
            safeRequest("vm.registered.list"),
            safeRequest("storage.list"),
            safeRequest("job.list"),
            safeRequest("run.list", {
                limit: 5,
                offset: 0
            }),
            safeRequest("recovery.list")
        ]);

        const model =
            VmbackupModel.deriveModel(
                {
                    status: status,
                    inventory: inventory,
                    registeredVms: registeredVms,
                    storage: storage,
                    jobs: jobs,
                    runs: runPage.runs || [],
                    runPage: runPage,
                    recovery: recovery
                },
                new Date()
            );

        console.log(
            "MODEL CREATED",
            model
        );

        console.log(
            "MODEL KEYS",
            Object.keys(model)
        );

        console.log(
            "MODEL VALUES",
            {
                successfulToday: model.successfulToday,
                failedToday: model.failedToday,
                active: model.active,
                recoveryRequired: model.recoveryRequired,
            }
        );

        VmbackupViews.renderModel(model);
    }

    start().catch(
        e => console.error(
            "MAIN FAILED",
            e
        )
    );

})();
