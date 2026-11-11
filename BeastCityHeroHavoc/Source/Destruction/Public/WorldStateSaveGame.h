#pragma once

#include "CoreMinimal.h"
#include "GameFramework/SaveGame.h"
#include "WorldStateSaveGame.generated.h"

// Persists which buildings (by BuildingId) have been destroyed, so reloading the world
// restores prior destruction instead of resetting it.
UCLASS()
class DESTRUCTION_API UWorldStateSaveGame : public USaveGame
{
	GENERATED_BODY()

public:
	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "World")
	TArray<FName> DestroyedBuildingIds;
};
