(function () {
    "use strict";

    console.log("MAIN CONTROLLER START");

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

        const model =
            VmbackupModel.deriveModel(
                {
                    status: status,
                    discoveredVms: [],
                    registeredVms: [],
                    storage: [],
                    jobs: [],
                    recovery: []
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
